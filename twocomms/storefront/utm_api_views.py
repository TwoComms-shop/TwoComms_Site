"""
API Views для UTM Диспетчера.

Предоставляет REST API endpoints для:
- Получения статистики в реальном времени (AJAX)
- Экспорта данных в CSV/Excel
- Детального просмотра сессий
- Сравнения периодов
"""

import csv
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.contrib.admin.views.decorators import staff_member_required
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser

from .models import UTMSession, UserAction
from .utm_analytics import (
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
    calculate_roi,
)
from .utm_cohort_analysis import (
    get_cohort_analysis,
    get_source_ltv_comparison,
    get_repeat_purchase_rate,
    get_campaign_ab_test_results,
)

logger = logging.getLogger(__name__)


class UTMAnalyticsViewSet(viewsets.ViewSet):
    """
    ViewSet для UTM аналитики.

    Endpoints:
    - GET /api/utm/general-stats/?period=today
    - GET /api/utm/sources/?period=week&limit=20
    - GET /api/utm/campaigns/?period=month&source=facebook
    - GET /api/utm/funnel/?period=today
    - GET /api/utm/sessions/{id}/
    - GET /api/utm/export-csv/?period=week&type=sources
    - GET /api/utm/compare/?period=today
    """
    permission_classes = [IsAdminUser]

    @action(detail=False, methods=['get'])
    def general_stats(self, request):
        """Получить общую статистику"""
        period = request.query_params.get('period', 'today')
        try:
            stats = get_general_stats(period)
            # Преобразуем Decimal в float для JSON
            for key, value in stats.items():
                if isinstance(value, Decimal):
                    stats[key] = float(value)
            return Response(stats)
        except Exception as e:
            logger.error(f"Error getting general stats: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def sources(self, request):
        """Получить статистику по источникам"""
        period = request.query_params.get('period', 'today')
        limit = int(request.query_params.get('limit', 20))
        try:
            sources = get_sources_stats(period, limit=limit)
            # Преобразуем Decimal в float
            for source in sources:
                for key, value in source.items():
                    if isinstance(value, Decimal):
                        source[key] = float(value)
            return Response(sources)
        except Exception as e:
            logger.error(f"Error getting sources stats: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def campaigns(self, request):
        """Получить статистику по кампаниям"""
        period = request.query_params.get('period', 'today')
        source_filter = request.query_params.get('source', None)
        limit = int(request.query_params.get('limit', 20))
        try:
            campaigns = get_campaigns_stats(period, source_filter=source_filter, limit=limit)
            # Преобразуем Decimal в float
            for campaign in campaigns:
                for key, value in campaign.items():
                    if isinstance(value, Decimal):
                        campaign[key] = float(value)
            return Response(campaigns)
        except Exception as e:
            logger.error(f"Error getting campaigns stats: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def content(self, request):
        """Получить статистику по контенту/креативам"""
        period = request.query_params.get('period', 'today')
        campaign_filter = request.query_params.get('campaign', None)
        limit = int(request.query_params.get('limit', 20))
        try:
            content = get_content_stats(period, campaign_filter=campaign_filter, limit=limit)
            # Преобразуем Decimal в float
            for item in content:
                for key, value in item.items():
                    if isinstance(value, Decimal):
                        item[key] = float(value)
            return Response(content)
        except Exception as e:
            logger.error(f"Error getting content stats: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def funnel(self, request):
        """Получить статистику воронки конверсий"""
        period = request.query_params.get('period', 'today')
        try:
            funnel = get_funnel_stats(period)
            return Response(funnel)
        except Exception as e:
            logger.error(f"Error getting funnel stats: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def geo(self, request):
        """Получить географическую статистику"""
        period = request.query_params.get('period', 'today')
        limit = int(request.query_params.get('limit', 15))
        try:
            geo = get_geo_stats(period, limit=limit)
            return Response(geo)
        except Exception as e:
            logger.error(f"Error getting geo stats: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def devices(self, request):
        """Получить статистику по устройствам"""
        period = request.query_params.get('period', 'today')
        try:
            devices = get_device_stats(period)
            return Response(devices)
        except Exception as e:
            logger.error(f"Error getting device stats: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def browsers(self, request):
        """Получить статистику по браузерам"""
        period = request.query_params.get('period', 'today')
        limit = int(request.query_params.get('limit', 10))
        try:
            browsers = get_browser_stats(period, limit=limit)
            return Response(browsers)
        except Exception as e:
            logger.error(f"Error getting browser stats: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def operating_systems(self, request):
        """Получить статистику по операционным системам"""
        period = request.query_params.get('period', 'today')
        limit = int(request.query_params.get('limit', 10))
        try:
            os_stats = get_os_stats(period, limit=limit)
            return Response(os_stats)
        except Exception as e:
            logger.error(f"Error getting OS stats: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def returning_visitors(self, request):
        """Получить статистику по возвратам"""
        period = request.query_params.get('period', 'today')
        try:
            returning = get_returning_visitors_stats(period)
            return Response(returning)
        except Exception as e:
            logger.error(f"Error getting returning visitors stats: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def sessions(self, request):
        """Получить последние сессии"""
        period = request.query_params.get('period', 'today')
        limit = int(request.query_params.get('limit', 50))
        try:
            sessions = get_recent_sessions(period, limit=limit)
            sessions_payload = []
            for session in sessions:
                sessions_payload.append({
                    'id': session.id,
                    'session_key': session.session_key,
                    'utm_source': session.utm_source,
                    'utm_medium': session.utm_medium,
                    'utm_campaign': session.utm_campaign,
                    'utm_content': session.utm_content,
                    'utm_term': session.utm_term,
                    'ip_address': session.ip_address,
                    'country': session.country,
                    'country_name': session.country_name,
                    'city': session.city,
                    'device_type': session.device_type,
                    'os_name': session.os_name,
                    'browser_name': session.browser_name,
                    'first_seen': session.first_seen.isoformat() if session.first_seen else None,
                    'last_seen': session.last_seen.isoformat() if session.last_seen else None,
                    'is_converted': session.is_converted,
                    'conversion_type': session.conversion_type,
                    'converted_at': session.converted_at.isoformat() if session.converted_at else None,
                })
            return Response(sessions_payload)
        except Exception as e:
            logger.error(f"Error getting sessions: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['get'])
    def session_detail(self, request, pk=None):
        """Получить детальную информацию о сессии"""
        try:
            session = UTMSession.objects.get(pk=pk)

            # Получаем действия пользователя
            actions = UserAction.objects.filter(utm_session=session).order_by('-timestamp')[:50]

            # Получаем заказы
            from orders.models import Order
            orders = Order.objects.filter(utm_session=session)

            data = {
                'session': {
                    'id': session.id,
                    'session_key': session.session_key,
                    'utm_source': session.utm_source,
                    'utm_medium': session.utm_medium,
                    'utm_campaign': session.utm_campaign,
                    'utm_content': session.utm_content,
                    'utm_term': session.utm_term,
                    'ip_address': session.ip_address,
                    'country': session.country,
                    'country_name': session.country_name,
                    'city': session.city,
                    'device_type': session.device_type,
                    'device_brand': session.device_brand,
                    'os_name': session.os_name,
                    'browser_name': session.browser_name,
                    'first_seen': session.first_seen.isoformat(),
                    'last_seen': session.last_seen.isoformat(),
                    'visit_count': session.visit_count,
                    'is_converted': session.is_converted,
                    'conversion_type': session.conversion_type,
                    'converted_at': session.converted_at.isoformat() if session.converted_at else None,
                    'landing_page': session.landing_page,
                    'referrer': session.referrer,
                },
                'actions': [
                    {
                        'id': action.id,
                        'action_type': action.action_type,
                        'action_type_display': action.get_action_type_display(),
                        'page_path': action.page_path,
                        'product_id': action.product_id,
                        'product_name': action.product_name,
                        'cart_value': float(action.cart_value) if action.cart_value else None,
                        'order_number': action.order_number,
                        'points_earned': action.points_earned,
                        'timestamp': action.timestamp.isoformat(),
                    }
                    for action in actions
                ],
                'orders': [
                    {
                        'id': order.id,
                        'order_number': order.order_number,
                        'total_sum': float(order.total_sum),
                        'payment_status': order.payment_status,
                        'status': order.status,
                        'created': order.created.isoformat(),
                    }
                    for order in orders
                ],
            }

            return Response(data)

        except UTMSession.DoesNotExist:
            return Response(
                {'error': 'Session not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Error getting session detail: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def compare(self, request):
        """Сравнить текущий период с предыдущим"""
        period = request.query_params.get('period', 'today')
        try:
            from .utm_analytics import compare_periods
            comparison = compare_periods(period)

            # Преобразуем Decimal в float
            for key in ['current', 'previous']:
                if key in comparison:
                    for stat_key, value in comparison[key].items():
                        if isinstance(value, Decimal):
                            comparison[key][stat_key] = float(value)

            if 'change' in comparison:
                for stat_key, value in comparison['change'].items():
                    if isinstance(value, Decimal):
                        comparison['change'][stat_key] = float(value)

            return Response(comparison)
        except Exception as e:
            logger.error(f"Error comparing periods: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def roi(self, request):
        """Рассчитать ROI для периода"""
        period = request.query_params.get('period', 'today')
        ad_spend = request.query_params.get('ad_spend', '0')

        try:
            ad_spend = Decimal(ad_spend)
            roi_data = calculate_roi(period, ad_spend)

            # Преобразуем Decimal в float
            for key, value in roi_data.items():
                if isinstance(value, Decimal):
                    roi_data[key] = float(value)

            return Response(roi_data)
        except Exception as e:
            logger.error(f"Error calculating ROI: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def cohort_analysis(self, request):
        """Когортный анализ (retention, ltv, orders, revenue)"""
        cohort_type = request.query_params.get('cohort_type', 'week')
        metric = request.query_params.get('metric', 'retention')
        utm_source = request.query_params.get('utm_source')
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')

        try:
            if start_date_str:
                start_date = datetime.fromisoformat(start_date_str)
                if timezone.is_naive(start_date):
                    start_date = timezone.make_aware(start_date)
            else:
                start_date = timezone.now() - timedelta(days=90)

            if end_date_str:
                end_date = datetime.fromisoformat(end_date_str)
                if timezone.is_naive(end_date):
                    end_date = timezone.make_aware(end_date)
            else:
                end_date = timezone.now()

            data = get_cohort_analysis(
                start_date=start_date,
                end_date=end_date,
                cohort_type=cohort_type,
                metric=metric,
                utm_source=utm_source
            )

            # Преобразуем Decimal в float
            for total in data.get('totals', []):
                for key, value in total.items():
                    if isinstance(value, Decimal):
                        total[key] = float(value)

            return Response(data)
        except Exception as e:
            logger.error(f"Error getting cohort analysis: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def ltv_comparison(self, request):
        """Сравнение LTV по источникам"""
        period = request.query_params.get('period', 'month')
        try:
            data = get_source_ltv_comparison(period)
            for item in data:
                for key, value in item.items():
                    if isinstance(value, Decimal):
                        item[key] = float(value)
            return Response(data)
        except Exception as e:
            logger.error(f"Error getting LTV comparison: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def repeat_rate(self, request):
        """Расчет повторных покупок"""
        period = request.query_params.get('period', 'month')
        utm_source = request.query_params.get('utm_source')
        try:
            data = get_repeat_purchase_rate(period, utm_source)
            return Response(data)
        except Exception as e:
            logger.error(f"Error getting repeat rate: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def ab_test(self, request):
        """Результаты A/B тестирования по кампании"""
        campaign = request.query_params.get('campaign')
        period = request.query_params.get('period', 'month')
        if not campaign:
            return Response(
                {'error': 'campaign параметр обязателен'},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            data = get_campaign_ab_test_results(campaign, period)
            for variant in data.get('variants', []):
                for key, value in variant.items():
                    if isinstance(value, Decimal):
                        variant[key] = float(value)
            return Response(data)
        except Exception as e:
            logger.error(f"Error getting A/B test results: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


@staff_member_required
@require_http_methods(["GET"])
def export_utm_csv(request):
    """
    Экспорт данных UTM в CSV.

    Параметры:
    - period: today/week/month/all_time
    - type: sessions/sources/campaigns/content
    """
    period = request.GET.get('period', 'today')
    export_type = request.GET.get('type', 'sessions')

    try:
        # Создаем CSV response
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="utm_{export_type}_{period}_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'

        # Добавляем BOM для правильного отображения в Excel
        response.write('\ufeff')

        writer = csv.writer(response)

        if export_type == 'sessions':
            # Экспорт сессий
            from .utm_analytics import filter_by_period
            sessions_qs = UTMSession.objects.all()
            sessions_qs = filter_by_period(sessions_qs, 'first_seen', period)
            sessions_qs = sessions_qs.select_related('session').prefetch_related('orders')[:1000]

            # Заголовки
            writer.writerow([
                'ID',
                'Джерело (utm_source)',
                'Канал (utm_medium)',
                'Кампанія (utm_campaign)',
                'Контент (utm_content)',
                'Ключове слово (utm_term)',
                'IP',
                'Країна',
                'Місто',
                'Пристрій',
                'ОС',
                'Браузер',
                'Перший візит',
                'Останній візит',
                'Кількість візитів',
                'Конверсія',
                'Тип конверсії',
                'Дата конверсії',
            ])

            # Данные
            for session in sessions_qs:
                writer.writerow([
                    session.id,
                    session.utm_source or 'direct',
                    session.utm_medium or 'none',
                    session.utm_campaign or '',
                    session.utm_content or '',
                    session.utm_term or '',
                    session.ip_address or '',
                    session.country_name or '',
                    session.city or '',
                    session.device_type or '',
                    session.os_name or '',
                    session.browser_name or '',
                    session.first_seen.strftime('%Y-%m-%d %H:%M:%S'),
                    session.last_seen.strftime('%Y-%m-%d %H:%M:%S'),
                    session.visit_count,
                    'Так' if session.is_converted else 'Ні',
                    session.conversion_type or '',
                    session.converted_at.strftime('%Y-%m-%d %H:%M:%S') if session.converted_at else '',
                ])

        elif export_type == 'sources':
            # Экспорт по источникам
            sources = get_sources_stats(period, limit=100)

            # Заголовки
            writer.writerow([
                'Джерело',
                'Сесії',
                'Конверсії',
                'CR%',
                'Дохід (грн)',
                'Середній чек (грн)',
                'Бали',
            ])

            # Данные
            for source in sources:
                writer.writerow([
                    source.get('utm_source', 'direct'),
                    source.get('sessions', 0),
                    source.get('conversions', 0),
                    source.get('conversion_rate', 0),
                    source.get('revenue', 0),
                    source.get('avg_order_value', 0),
                    source.get('total_score', 0),
                ])

        elif export_type == 'campaigns':
            # Экспорт по кампаниям
            campaigns = get_campaigns_stats(period, limit=100)

            # Заголовки
            writer.writerow([
                'Джерело',
                'Канал',
                'Кампанія',
                'Сесії',
                'Конверсії',
                'CR%',
                'Дохід (грн)',
                'Середній чек (грн)',
            ])

            # Данные
            for campaign in campaigns:
                writer.writerow([
                    campaign.get('utm_source', ''),
                    campaign.get('utm_medium', ''),
                    campaign.get('utm_campaign', ''),
                    campaign.get('sessions', 0),
                    campaign.get('conversions', 0),
                    campaign.get('conversion_rate', 0),
                    campaign.get('revenue', 0),
                    campaign.get('avg_order_value', 0),
                ])

        elif export_type == 'content':
            # Экспорт по контенту
            content = get_content_stats(period, limit=100)

            # Заголовки
            writer.writerow([
                'Джерело',
                'Кампанія',
                'Контент/Креатив',
                'Сесії',
                'Конверсії',
                'CR%',
                'Дохід (грн)',
            ])

            # Данные
            for item in content:
                writer.writerow([
                    item.get('utm_source', ''),
                    item.get('utm_campaign', ''),
                    item.get('utm_content', ''),
                    item.get('sessions', 0),
                    item.get('conversions', 0),
                    item.get('conversion_rate', 0),
                    item.get('revenue', 0),
                ])

        return response

    except Exception as e:
        logger.error(f"Error exporting CSV: {e}", exc_info=True)
        return JsonResponse(
            {'error': str(e)},
            status=500
        )
