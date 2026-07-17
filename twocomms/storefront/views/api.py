"""
API views - AJAX и JSON endpoints.

Содержит views для:
- AJAX запросов от фронтенда
- JSON API endpoints
- Реал-тайм данные
- Аналитика и трекинг
"""

import json
import logging

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.utils.translation import gettext as _
from django_ratelimit.decorators import ratelimit

from ..models import CustomPrintLead, Product, SurveySession, UserAction
from ..services.catalog_helpers import get_categories_cached
from ..utm_tracking import record_custom_print_event, record_search, record_survey_event, record_user_action
from cache_utils import get_fragment_cache


logger = logging.getLogger('storefront.analytics')
SERVER_ONLY_EVENT_TYPES = frozenset({'lead', 'purchase'})


# ==================== API ENDPOINTS ====================


@require_http_methods(["GET"])
def analytics_bootstrap(request):
    """
    W3-3 (SEO-010): ленивая выдача идентификационных кук.

    Анонимные GET HTML-страниц больше НЕ ставят csrftoken/twc_vid
    (Set-Cookie выключал LiteSpeed page cache → холодный TTFB 8-18s).
    Вместо этого base.html при отсутствии csrftoken-cookie дергает этот
    endpoint: он ставит csrftoken (через get_token) и analytics-куки
    (AnalyticsIdentityMiddleware знает этот путь как разрешённый).
    Ответ всегда no-store — кэшировать его нельзя.
    """
    from django.middleware.csrf import get_token

    get_token(request)  # помечает CSRF_COOKIE_USED → middleware поставит cookie
    response = JsonResponse({'ok': True})
    response['Cache-Control'] = 'no-store, private'
    return response


@csrf_exempt
@require_http_methods(["POST"])
@ratelimit(key='ip', rate='10/m', method='POST', block=False)
def client_error(request):
    """
    W3-2 (TECH-041): приёмник клиентских JS-ошибок.

    base.html вешает window.onerror/unhandledrejection и шлёт сюда краткий
    отчёт. Пишем в отдельный лог client_errors.log (logger
    storefront.client_errors, без Telegram-алертов — фронтовые ошибки
    массовые и не должны флудить админа). Rate-limit 10/m/IP + жёсткая
    обрезка полей: это публичный endpoint.
    """
    if getattr(request, 'limited', False):
        return JsonResponse({'ok': False}, status=429)
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'ok': False}, status=400)

    def _s(key, limit):
        value = payload.get(key)
        return str(value)[:limit] if value is not None else ''

    logging.getLogger('storefront.client_errors').info(
        'msg=%s | url=%s | src=%s:%s | ua=%s',
        _s('message', 500), _s('url', 300),
        _s('source', 200), _s('line', 10),
        (request.META.get('HTTP_USER_AGENT') or '')[:200],
    )
    response = JsonResponse({'ok': True})
    response['Cache-Control'] = 'no-store, private'
    return response


def _public_product_queryset(request):
    """
    Возвращает queryset товаров c учетом статуса публикации.
    """
    qs = Product.objects.select_related('category')
    if not (request.user.is_authenticated and request.user.is_staff):
        qs = qs.filter(status='published')
    return qs


@require_http_methods(["GET"])
def get_product_json(request, product_id):
    """
    Получить данные товара в формате JSON.

    Args:
        product_id (int): ID товара

    Returns:
        JsonResponse: Полные данные товара
    """
    try:
        product = _public_product_queryset(request).get(id=product_id)

        data = {
            'id': product.id,
            'title': product.title,
            'slug': product.slug,
            'price': product.price,
            'final_price': product.final_price,
            'has_discount': product.has_discount,
            'discount_percent': product.discount_percent,
            'description': product.description,
            'category': {
                'id': product.category.id if product.category else None,
                'name': product.category.name if product.category else None,
                'slug': product.category.slug if product.category else None
            },
            'main_image': product.main_image.url if product.main_image else None,
            'featured': product.featured
        }

        return JsonResponse({
            'success': True,
            'product': data
        })

    except Product.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Product not found'
        }, status=404)


@require_http_methods(["GET"])
def get_categories_json(request):
    """
    Получить список всех категорий в формате JSON.

    Returns:
        JsonResponse: Список категорий
    """
    fragment_cache = get_fragment_cache()
    categories = get_categories_cached(fragment_cache)

    categories_data = [
        {
            'id': cat.id,
            'name': cat.name,
            'slug': cat.slug,
            'order': cat.order,
            'icon': cat.icon.url if cat.icon else None,
            'cover': cat.cover.url if cat.cover else None,
            'is_active': cat.is_active,
            'is_featured': cat.is_featured
        }
        for cat in categories
    ]

    return JsonResponse({
        'success': True,
        'categories': categories_data,
        'count': len(categories_data)
    })


@require_http_methods(["POST"])
@csrf_exempt  # Для внешних интеграций
def track_event(request):
    """
    Трекинг событий для аналитики.

    POST params:
        event_type: Тип события (view, click, add_to_cart, etc.)
        product_id: ID товара (опционально)
        category_id: ID категории (опционально)
        metadata: Дополнительные данные (JSON)

    Returns:
        JsonResponse: success
    """
    try:
        data = json.loads(request.body)
        event_type = data.get('event_type')
        product_id = data.get('product_id')
        category_id = data.get('category_id')
        metadata = data.get('metadata', {})
        if not isinstance(metadata, dict):
            metadata = {}

        if not event_type:
            return JsonResponse({
                'success': False,
                'error': 'event_type is required'
            }, status=400)

        allowed_event_types = {choice[0] for choice in UserAction.ACTION_TYPES}
        if event_type not in allowed_event_types:
            return JsonResponse({
                'success': False,
                'error': f'Unsupported event_type: {event_type}'
            }, status=400)
        if event_type in SERVER_ONLY_EVENT_TYPES:
            return JsonResponse({
                'success': False,
                'error': f'Server-only event_type: {event_type}',
            }, status=400)

        if product_id in ("", None):
            product_id = None
        elif not isinstance(product_id, int):
            try:
                product_id = int(product_id)
            except (TypeError, ValueError):
                product_id = None

        if category_id is not None:
            metadata.setdefault('category_id', category_id)

        product_name = metadata.get('product_name')
        if product_id and not product_name:
            product_name = Product.objects.filter(pk=product_id).values_list('title', flat=True).first()

        action = None
        if event_type == 'search':
            query = (metadata.get('query') or data.get('query') or '').strip()
            if not query:
                return JsonResponse({
                    'success': False,
                    'error': 'query is required for search events'
                }, status=400)
            action = record_search(request, query)
        elif event_type.startswith('custom_print_'):
            lead = None
            lead_id = metadata.get('lead_id') or data.get('lead_id')
            if lead_id:
                try:
                    lead = CustomPrintLead.objects.filter(pk=int(lead_id)).first()
                except (TypeError, ValueError):
                    lead = None
            action = record_custom_print_event(
                request,
                event_type,
                lead=lead,
                step_key=metadata.get('step_key'),
                metadata=metadata,
            )
        elif event_type.startswith('survey_'):
            session = None
            session_id = metadata.get('survey_session_id') or data.get('survey_session_id')
            if session_id:
                try:
                    session = SurveySession.objects.filter(pk=int(session_id)).first()
                except (TypeError, ValueError):
                    session = None
            action = record_survey_event(
                request,
                event_type,
                session=session,
                question_id=metadata.get('question_id') or data.get('question_id'),
                metadata=metadata,
            )
        else:
            action = record_user_action(
                request,
                action_type=event_type,
                product_id=product_id,
                product_name=product_name,
                cart_value=data.get('cart_value'),
                order_id=data.get('order_id'),
                order_number=data.get('order_number'),
                metadata=metadata,
            )

        logger.info("Event tracked: %s, product=%s, category=%s", event_type, product_id, category_id)

        return JsonResponse({
            'success': True,
            'message': 'Event tracked',
            'stored': bool(action)
        })

    except Exception as e:
        logger.exception("track_event failed")
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@require_http_methods(["POST"])
@csrf_exempt  # RUM маяк идёт navigator.sendBeacon — без CSRF токена
def rum_beacon(request):
    """
    Real User Monitoring beacon — приёмник Core Web Vitals с клиента.

    Принимает JSON (или form-data c параметром `payload`) вида:
        {
          "url": "https://twocomms.shop/",
          "nav_type": "navigate|reload|back_forward",
          "device_class": "low|mid|high",
          "conn": "4g|3g|2g|slow-2g|unknown",
          "metrics": {
            "LCP": 2400, "CLS": 0.03, "INP": 180, "FCP": 1100, "TTFB": 220, "FID": 45
          },
          "ua_mobile": true
        }

    Не сохраняет в БД — пишет в 'storefront.rum' logger (по умолчанию stdout/файл,
    дальше можно перенаправить в ELK/Sentry/CloudWatch через настройки логирования).
    Тихо возвращает 204 — клиенту не нужен ответ; sendBeacon игнорирует тело.
    """
    import json
    import logging

    logger = logging.getLogger('storefront.rum')

    try:
        raw = request.body or b''
        # sendBeacon с type=application/json кладёт тело напрямую;
        # fetch-fallback может приходить как form-data c ключом payload
        if raw:
            try:
                data = json.loads(raw.decode('utf-8', errors='replace'))
            except (ValueError, UnicodeDecodeError):
                data = {}
        else:
            data = {}
        if not data and request.POST:
            try:
                data = json.loads(request.POST.get('payload', '{}'))
            except (ValueError, TypeError):
                data = {}

        metrics = data.get('metrics') or {}
        if not isinstance(metrics, dict):
            metrics = {}
        # Огрубляем и не логируем ничего, кроме ожидаемых полей — защита от мусора
        allowed = {'LCP', 'CLS', 'INP', 'FCP', 'TTFB', 'FID'}
        safe_metrics = {k: metrics[k] for k in allowed if k in metrics}

        logger.info(
            'rum url=%s nav=%s dc=%s conn=%s mobile=%s metrics=%s',
            str(data.get('url') or '')[:200],
            str(data.get('nav_type') or '')[:16],
            str(data.get('device_class') or '')[:8],
            str(data.get('conn') or '')[:12],
            bool(data.get('ua_mobile')),
            safe_metrics,
        )
    except Exception:
        # RUM никогда не должен 5xx-ить — тихо глотаем
        logger.debug('rum beacon parse failed', exc_info=True)

    # 204 No Content — минимальный оверхед, sendBeacon не читает тело
    return JsonResponse({}, status=204)


@require_http_methods(["GET"])
def search_suggestions(request):
    """
    Автодополнение для поиска (AJAX).

    Query params:
        q: Поисковый запрос
        limit: Количество результатов (по умолчанию 5)

    Returns:
        JsonResponse: Список предложений
    """
    query = request.GET.get('q', '').strip()
    limit = int(request.GET.get('limit', 5))

    if not query or len(query) < 2:
        return JsonResponse({
            'success': True,
            'suggestions': []
        })

    # Ищем по началу названия (быстрее чем contains)
    products = _public_product_queryset(request).filter(
        title__istartswith=query
    ).values('id', 'title', 'slug')[:limit]

    suggestions = [
        {
            'id': p['id'],
            'title': p['title'],
            'slug': p['slug']
        }
        for p in products
    ]

    return JsonResponse({
        'success': True,
        'suggestions': suggestions,
        'count': len(suggestions)
    })


@require_http_methods(["GET"])
def product_availability(request, product_id):
    """
    Проверка доступности товара.

    Args:
        product_id (int): ID товара

    Returns:
        JsonResponse: available, in_stock
    """
    try:
        qs = _public_product_queryset(request)
        product = qs.get(id=product_id)

        # TODO: Добавить реальную проверку наличия на складе
        # Пока просто возвращаем True

        return JsonResponse({
            'success': True,
            'available': True,
            'in_stock': True,
            'message': _('Товар доступний')
        })

    except Product.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': _('Товар не знайдено')
        }, status=404)


@require_http_methods(["GET"])
def get_related_products(request, product_id):
    """
    Получить похожие товары.

    Args:
        product_id (int): ID товара

    Returns:
        JsonResponse: Список похожих товаров
    """
    try:
        qs = _public_product_queryset(request)
        product = qs.get(id=product_id)

        # Ищем товары из той же категории
        related = qs.filter(
            category=product.category
        ).exclude(
            id=product_id
        ).select_related('category')[:6]

        related_data = [
            {
                'id': p.id,
                'title': p.title,
                'slug': p.slug,
                'price': p.price,
                'final_price': p.final_price,
                'main_image': p.main_image.url if p.main_image else None
            }
            for p in related
        ]

        return JsonResponse({
            'success': True,
            'products': related_data,
            'count': len(related_data)
        })

    except Product.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': _('Товар не знайдено')
        }, status=404)


@require_http_methods(["POST"])
def newsletter_subscribe(request):
    """
    Подписка на рассылку.

    POST params:
        email: Email адрес

    Returns:
        JsonResponse: success, message
    """
    import json

    try:
        data = json.loads(request.body)
        email = data.get('email', '').strip()

        if not email:
            return JsonResponse({
                'success': False,
                'error': _('Email обов\'язковий')
            }, status=400)

        # TODO: Сохранить email в БД или отправить в сервис рассылок
        # Например: MailChimp, SendGrid, etc.

        return JsonResponse({
            'success': True,
            'message': _('Дякуємо за підписку!')
        })

    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@require_http_methods(["POST"])
def contact_form(request):
    """
    Форма обратной связи.

    POST params:
        name: Имя
        email: Email
        phone: Телефон
        message: Сообщение

    Returns:
        JsonResponse: success, message
    """
    import json

    try:
        data = json.loads(request.body)
        name = data.get('name', '').strip()
        email = data.get('email', '').strip()
        phone = data.get('phone', '').strip()
        message = data.get('message', '').strip()

        if not all([name, email, message]):
            return JsonResponse({
                'success': False,
                'error': _('Всі поля обов\'язкові')
            }, status=400)

        # TODO: Отправить email администратору или сохранить в БД

        return JsonResponse({
            'success': True,
            'message': _('Ваше повідомлення надіслано!')
        })

    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)
