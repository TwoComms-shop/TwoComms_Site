"""
UTM Tracking Middleware.

Захватывает и сохраняет UTM-параметры из URL в сессию пользователя.
Создает или обновляет UTMSession для детальной аналитики рекламных кампаний.

Middleware должен быть размещен ПЕРЕД SimpleAnalyticsMiddleware в settings.MIDDLEWARE.
"""

import logging
from django.utils.deprecation import MiddlewareMixin
from django.utils import timezone
from django.db import transaction
from .analytics_exclusions import is_request_excluded
from .analytics_noise import is_analytics_noise_path
from .models import UTMSession, SiteSession
from .utm_utils import (
    get_client_ip,
    get_geolocation,
    parse_user_agent,
    sanitize_utm_param,
    is_bot_user_agent,
    normalize_utm_source,
    detect_ai_source,
    AI_SOURCES,
)

logger = logging.getLogger(__name__)


class UTMTrackingMiddleware(MiddlewareMixin):
    """
    Middleware для захвата и сохранения UTM-параметров.

    Захватывает:
    - Стандартные UTM-параметры (utm_source, utm_medium, utm_campaign, utm_content, utm_term)
    - Платформенные идентификаторы (fbclid, gclid, ttclid)
    - Cookies (_fbc, _fbp)
    - Геолокацию по IP
    - Информацию об устройстве и браузере из User-Agent

    Функциональность:
    - Создает UTMSession при первом визите с UTM
    - Обновляет существующую сессию при повторных визитах
    - Сохраняет UTM в Django session для использования при создании заказа
    - Пропускает ботов и служебные пути
    """

    UTM_PARAMS = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term']
    PLATFORM_PARAMS = ['fbclid', 'gclid', 'ttclid']
    def process_request(self, request):
        """Захватывает UTM-параметры из URL и сохраняет в сессию"""
        try:
            host = request.get_host().split(':')[0].lower()
            # DTF subdomain uses a separate funnel; skip storefront UTM writes.
            if host.startswith('dtf.'):
                return None

            # Пропускаем служебные пути
            if is_analytics_noise_path(request.path):
                return None

            # Пропускаем ботов (опционально)
            user_agent = request.META.get('HTTP_USER_AGENT', '')
            if is_bot_user_agent(user_agent):
                logger.debug(f"Skipping bot: {user_agent[:100]}")
                return None

            # Skip writes for explicitly excluded entities (admin office IPs, staff users, etc.).
            if is_request_excluded(request, user_agent=user_agent):
                return None

            # Получаем UTM-параметры из GET-запроса
            utm_data = {}
            platform_data = {}
            has_utm = False

            # Стандартные UTM-параметры
            for param in self.UTM_PARAMS:
                value = request.GET.get(param, '').strip()
                if value:
                    clean_value = sanitize_utm_param(value)
                    if clean_value:
                        utm_data[param] = clean_value
                        has_utm = True

            # W2-8 (AN-032): нормализация utm_source по словарю алиасов —
            # ig/Instagram/IGShopping/Inst_Vid схлопываются в 'instagram' и т.д.
            if utm_data.get('utm_source'):
                utm_data['utm_source'] = normalize_utm_source(utm_data['utm_source'])
                # AI-источник из utm → канал «AI», если medium не задан явно
                if utm_data['utm_source'] in AI_SOURCES and not utm_data.get('utm_medium'):
                    utm_data['utm_medium'] = 'ai'

            # W2-8 (AN-033): AI-трафик без UTM (chatgpt.com — 3-й источник)
            # детектим по referrer → синтетический utm_source + канал «AI».
            if not has_utm:
                ai_source = detect_ai_source(request.META.get('HTTP_REFERER', ''))
                if ai_source:
                    utm_data['utm_source'] = ai_source
                    utm_data['utm_medium'] = 'ai'
                    has_utm = True
                    logger.info(f"Detected AI referrer traffic: {ai_source}")

            # Платформенные идентификаторы
            for param in self.PLATFORM_PARAMS:
                value = request.GET.get(param, '').strip()
                if value:
                    platform_data[param] = value[:255]

            # Cookies для Meta
            fbc = request.COOKIES.get('_fbc', '')
            fbp = request.COOKIES.get('_fbp', '')
            if fbc:
                platform_data['fbc'] = fbc[:255]
            if fbp:
                platform_data['fbp'] = fbp[:255]

            # Если есть UTM-параметры, сохраняем их
            if has_utm:
                logger.info(f"Captured UTM: {utm_data}")

                # Сохраняем в сессию для последующего использования
                request.session['utm_data'] = utm_data
                request.session['platform_data'] = platform_data
                request.session['utm_first_seen'] = timezone.now().isoformat()
                request.session.modified = True

                # Создаем или обновляем UTMSession
                self._create_or_update_utm_session(request, utm_data, platform_data)

            # Если нет UTM в URL, но есть сохраненные в сессии - используем их
            elif 'utm_data' in request.session:
                utm_data = request.session['utm_data']
                platform_data = request.session.get('platform_data', {})
                logger.debug(f"Using stored UTM: {utm_data}")
            else:
                # Проверяем, есть ли существующая UTM-сессия для этого session_key
                # (для повторных визитов без UTM в URL)
                session_key = request.session.session_key
                if session_key:
                    try:
                        utm_session = UTMSession.objects.get(session_key=session_key)
                        # Обновляем last_seen и увеличиваем счетчик визитов
                        utm_session.increment_visit()
                        logger.debug(f"Updated existing UTM session: {utm_session}")
                    except UTMSession.DoesNotExist:
                        pass

            # Сохраняем в request для использования в views
            request.utm_data = utm_data
            request.platform_data = platform_data

        except Exception as e:
            # Логируем ошибку, но не прерываем запрос
            logger.error(f"UTM tracking error: {e}", exc_info=True)

        return None

    def _create_or_update_utm_session(self, request, utm_data, platform_data):
        """Создает или обновляет UTMSession с полной информацией"""
        try:
            session_key = request.session.session_key
            if not session_key:
                # Создаем сессию, если еще не создана
                request.session.save()
                session_key = request.session.session_key

            if not session_key:
                logger.warning("Could not create session_key")
                return

            # Получаем IP-адрес
            ip_address = get_client_ip(request)
            visitor_id = getattr(request, 'analytics_visitor_id', None)
            first_touch_data = getattr(request, 'analytics_first_touch_data', {}) or {}

            # Определяем геолокацию
            geo_data = {}
            if ip_address:
                geo_data = get_geolocation(ip_address)

            # Парсим User-Agent
            user_agent_str = request.META.get('HTTP_USER_AGENT', '')
            device_data = parse_user_agent(user_agent_str)

            # Создаем или обновляем UTM сессию
            with transaction.atomic():
                utm_session, created = UTMSession.objects.get_or_create(
                    session_key=session_key,
                    defaults={
                        'visitor_id': visitor_id,
                        # UTM параметры
                        'utm_source': utm_data.get('utm_source'),
                        'utm_medium': utm_data.get('utm_medium'),
                        'utm_campaign': utm_data.get('utm_campaign'),
                        'utm_content': utm_data.get('utm_content'),
                        'utm_term': utm_data.get('utm_term'),

                        # Платформенные идентификаторы
                        'fbclid': platform_data.get('fbclid'),
                        'gclid': platform_data.get('gclid'),
                        'ttclid': platform_data.get('ttclid'),
                        'fbc': platform_data.get('fbc'),
                        'fbp': platform_data.get('fbp'),

                        # Геолокация
                        'ip_address': ip_address,
                        'country': geo_data.get('country'),
                        'country_name': geo_data.get('country_name'),
                        'city': geo_data.get('city'),
                        'region': geo_data.get('region'),
                        'timezone': geo_data.get('timezone'),

                        # Устройство и браузер
                        'device_type': device_data.get('device_type'),
                        'device_brand': device_data.get('device_brand'),
                        'device_model': device_data.get('device_model'),
                        'os_name': device_data.get('os_name'),
                        'os_version': device_data.get('os_version'),
                        'browser_name': device_data.get('browser_name'),
                        'browser_version': device_data.get('browser_version'),
                        'user_agent': user_agent_str[:500] if user_agent_str else None,

                        # Дополнительные данные
                        'referrer': request.META.get('HTTP_REFERER', '')[:512] or None,
                        'landing_page': request.path[:512] or None,
                    }
                )

                if created:
                    logger.info(f"Created new UTM session: {utm_session}")
                else:
                    # Обновляем last_seen и увеличиваем счетчик визитов
                    utm_session.increment_visit()
                    updated_fields = []
                    if visitor_id and utm_session.visitor_id != visitor_id:
                        utm_session.visitor_id = visitor_id
                        updated_fields.append('visitor_id')
                    if ip_address and utm_session.ip_address != ip_address:
                        utm_session.ip_address = ip_address
                        updated_fields.append('ip_address')
                    if first_touch_data and utm_session.landing_page != (first_touch_data.get('landing_path') or utm_session.landing_page):
                        # keep the original landing_page when it already exists
                        if not utm_session.landing_page:
                            utm_session.landing_page = (first_touch_data.get('landing_path') or '')[:512] or None
                            updated_fields.append('landing_page')
                    if updated_fields:
                        utm_session.save(update_fields=updated_fields)
                    logger.info(f"Updated existing UTM session: {utm_session}")

                # Связываем с SiteSession, если она существует
                try:
                    site_session = SiteSession.objects.get(session_key=session_key)
                    if utm_session.session != site_session:
                        utm_session.session = site_session
                        utm_session.save(update_fields=['session'])
                        logger.debug(f"Linked UTM session to SiteSession")
                except SiteSession.DoesNotExist:
                    # SiteSession еще не создана, это нормально
                    # Она будет создана SimpleAnalyticsMiddleware позже
                    pass

                return utm_session

        except Exception as e:
            logger.error(f"Error creating/updating UTM session: {e}", exc_info=True)
            return None
