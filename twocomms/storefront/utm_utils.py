"""
Утилиты для UTM-трекинга и аналитики.
Включает функции для определения геолокации, парсинга User-Agent,
расчета баллов и другие вспомогательные функции.
"""

import logging
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def get_client_ip(request) -> Optional[str]:
    """
    Извлекает IP-адрес клиента из запроса.
    Учитывает прокси и балансировщики нагрузки.
    """
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        # Берем первый IP из списка (реальный IP клиента)
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR', '')

    # Валидация IP
    if ip and _is_valid_ip(ip):
        return ip
    return None


def _is_valid_ip(ip: str) -> bool:
    """Проверяет валидность IP-адреса (IPv4 или IPv6)"""
    # Простая проверка
    if not ip:
        return False
    # Проверка IPv4
    ipv4_pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
    if re.match(ipv4_pattern, ip):
        parts = ip.split('.')
        return all(0 <= int(part) <= 255 for part in parts)
    # IPv6 (упрощенная проверка)
    if ':' in ip:
        return True
    return False


def get_geolocation(ip_address: str) -> Dict[str, Optional[str]]:
    """
    Определяет геолокацию по IP-адресу.

    Использует:
    1. GeoIP2 (если установлен)
    2. Fallback на внешний API (опционально)
    3. Возвращает пустые значения, если определить не удалось

    Returns:
        dict: {
            'country': 'UA',
            'country_name': 'Ukraine',
            'city': 'Kyiv',
            'region': 'Kyiv City',
            'timezone': 'Europe/Kiev'
        }
    """
    result = {
        'country': None,
        'country_name': None,
        'city': None,
        'region': None,
        'timezone': None,
    }

    if not ip_address or ip_address == '127.0.0.1' or ip_address.startswith('192.168.'):
        return result

    # Попытка использовать GeoIP2
    try:
        from django.contrib.gis.geoip2 import GeoIP2
        g = GeoIP2()

        try:
            city_data = g.city(ip_address)
            if city_data:
                result['country'] = city_data.get('country_code')
                result['country_name'] = city_data.get('country_name')
                result['city'] = city_data.get('city')
                result['region'] = city_data.get('region')
                result['timezone'] = city_data.get('time_zone')
                return result
        except Exception as e:
            logger.debug(f"GeoIP2 city lookup failed for {ip_address}: {e}")

        # Fallback на country
        try:
            country_data = g.country(ip_address)
            if country_data:
                result['country'] = country_data.get('country_code')
                result['country_name'] = country_data.get('country_name')
                return result
        except Exception as e:
            logger.debug(f"GeoIP2 country lookup failed for {ip_address}: {e}")

    except ImportError:
        logger.debug("GeoIP2 not available")
    except Exception as e:
        logger.warning(f"GeoIP2 error: {e}")

    # Fallback: внешний API (опционально, закомментировано для производительности)
    # result = _get_geolocation_from_api(ip_address)

    return result


def _get_geolocation_from_api(ip_address: str) -> Dict[str, Optional[str]]:
    """
    Fallback: запрос геолокации через внешний API.
    Используется только если GeoIP2 недоступен.
    """
    result = {
        'country': None,
        'country_name': None,
        'city': None,
        'region': None,
        'timezone': None,
    }

    try:
        import requests
        # Используем бесплатный API (лимит: 15000 запросов/час)
        response = requests.get(
            f'http://ip-api.com/json/{ip_address}',
            timeout=2
        )
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'success':
                result['country'] = data.get('countryCode')
                result['country_name'] = data.get('country')
                result['city'] = data.get('city')
                result['region'] = data.get('regionName')
                result['timezone'] = data.get('timezone')
    except Exception as e:
        logger.debug(f"External API geolocation failed: {e}")

    return result


def parse_user_agent(user_agent_string: str) -> Dict[str, Optional[str]]:
    """
    Парсит User-Agent для определения устройства, ОС и браузера.

    Returns:
        dict: {
            'device_type': 'mobile|desktop|tablet|unknown',
            'device_brand': 'Apple',
            'device_model': 'iPhone',
            'os_name': 'iOS',
            'os_version': '14.0',
            'browser_name': 'Safari',
            'browser_version': '14.0'
        }
    """
    result = {
        'device_type': 'unknown',
        'device_brand': None,
        'device_model': None,
        'os_name': None,
        'os_version': None,
        'browser_name': None,
        'browser_version': None,
    }

    if not user_agent_string:
        return result

    ua_lower = user_agent_string.lower()

    # Попытка использовать библиотеку user-agents
    try:
        from user_agents import parse as ua_parse
        ua = ua_parse(user_agent_string)

        # Device type
        if ua.is_mobile:
            result['device_type'] = 'mobile'
        elif ua.is_tablet:
            result['device_type'] = 'tablet'
        elif ua.is_pc:
            result['device_type'] = 'desktop'
        else:
            result['device_type'] = 'unknown'

        # Device info
        if ua.device.family and ua.device.family != 'Other':
            result['device_brand'] = ua.device.brand or None
            result['device_model'] = ua.device.model or ua.device.family

        # OS info
        if ua.os.family and ua.os.family != 'Other':
            result['os_name'] = ua.os.family
            result['os_version'] = ua.os.version_string or None

        # Browser info
        if ua.browser.family and ua.browser.family != 'Other':
            result['browser_name'] = ua.browser.family
            result['browser_version'] = ua.browser.version_string or None

        return result

    except ImportError:
        logger.debug("user-agents library not available, using simple parsing")
    except Exception as e:
        logger.debug(f"user-agents parsing failed: {e}")

    # Fallback: простой парсинг
    result = _simple_parse_user_agent(user_agent_string, ua_lower)

    return result


def _simple_parse_user_agent(user_agent_string: str, ua_lower: str) -> Dict[str, Optional[str]]:
    """Упрощенный парсинг User-Agent без внешних библиотек"""
    result = {
        'device_type': 'desktop',
        'device_brand': None,
        'device_model': None,
        'os_name': None,
        'os_version': None,
        'browser_name': None,
        'browser_version': None,
    }

    # Device type
    if 'mobile' in ua_lower or 'android' in ua_lower or 'iphone' in ua_lower:
        result['device_type'] = 'mobile'
    elif 'tablet' in ua_lower or 'ipad' in ua_lower:
        result['device_type'] = 'tablet'

    # OS detection
    if 'windows' in ua_lower:
        result['os_name'] = 'Windows'
        if 'windows nt 10' in ua_lower:
            result['os_version'] = '10'
        elif 'windows nt 6.3' in ua_lower:
            result['os_version'] = '8.1'
    elif 'mac os x' in ua_lower or 'macos' in ua_lower:
        result['os_name'] = 'macOS'
    elif 'iphone' in ua_lower or 'ipad' in ua_lower:
        result['os_name'] = 'iOS'
        # Попытка извлечь версию
        match = re.search(r'os (\d+)[._](\d+)', ua_lower)
        if match:
            result['os_version'] = f"{match.group(1)}.{match.group(2)}"
    elif 'android' in ua_lower:
        result['os_name'] = 'Android'
        match = re.search(r'android (\d+\.?\d*)', ua_lower)
        if match:
            result['os_version'] = match.group(1)
    elif 'linux' in ua_lower:
        result['os_name'] = 'Linux'

    # Browser detection
    if 'chrome' in ua_lower and 'edg' not in ua_lower:
        result['browser_name'] = 'Chrome'
        match = re.search(r'chrome/(\d+\.?\d*)', ua_lower)
        if match:
            result['browser_version'] = match.group(1)
    elif 'safari' in ua_lower and 'chrome' not in ua_lower:
        result['browser_name'] = 'Safari'
        match = re.search(r'version/(\d+\.?\d*)', ua_lower)
        if match:
            result['browser_version'] = match.group(1)
    elif 'firefox' in ua_lower:
        result['browser_name'] = 'Firefox'
        match = re.search(r'firefox/(\d+\.?\d*)', ua_lower)
        if match:
            result['browser_version'] = match.group(1)
    elif 'edg' in ua_lower:
        result['browser_name'] = 'Edge'
        match = re.search(r'edg/(\d+\.?\d*)', ua_lower)
        if match:
            result['browser_version'] = match.group(1)

    # Device brand/model for mobile
    if result['device_type'] in ['mobile', 'tablet']:
        if 'iphone' in ua_lower:
            result['device_brand'] = 'Apple'
            result['device_model'] = 'iPhone'
        elif 'ipad' in ua_lower:
            result['device_brand'] = 'Apple'
            result['device_model'] = 'iPad'
        elif 'samsung' in ua_lower:
            result['device_brand'] = 'Samsung'
        elif 'huawei' in ua_lower:
            result['device_brand'] = 'Huawei'
        elif 'xiaomi' in ua_lower:
            result['device_brand'] = 'Xiaomi'

    return result


def calculate_action_points(action_type: str, **kwargs) -> int:
    """
    Рассчитывает баллы за действие пользователя.

    Args:
        action_type: Тип действия (page_view, product_view, add_to_cart, etc.)
        **kwargs: Дополнительные параметры (cart_value, order_value, etc.)

    Returns:
        int: Количество баллов
    """
    points_map = {
        'page_view': 1,
        'product_view': 3,
        'add_to_cart': 10,
        'remove_from_cart': -5,
        'initiate_checkout': 20,
        'lead': 50,  # Предоплата
        'purchase': 100,  # Полная оплата
        'search': 2,
        'click': 1,
        'scroll': 0,
        'time_on_page': 0,
    }

    base_points = points_map.get(action_type, 0)

    # Бонусные баллы за высокий чек
    if action_type in ['lead', 'purchase']:
        order_value = kwargs.get('cart_value', 0) or kwargs.get('order_value', 0)
        if order_value:
            # +1 балл за каждые 100 грн
            bonus = int(float(order_value) / 100)
            base_points += bonus

    return base_points


def sanitize_utm_param(value: str, max_length: int = 255) -> Optional[str]:
    """
    Очищает и валидирует UTM-параметр.

    Args:
        value: Значение параметра
        max_length: Максимальная длина

    Returns:
        str или None: Очищенное значение или None
    """
    if not value:
        return None

    # Убираем лишние пробелы
    value = value.strip()

    if not value:
        return None

    # Ограничиваем длину
    if len(value) > max_length:
        value = value[:max_length]

    # Удаляем потенциально опасные символы
    # Разрешаем: буквы, цифры, дефис, подчеркивание, точку
    value = re.sub(r'[^\w\-\.]', '_', value)

    return value or None


# ---------------------------------------------------------------------------
# W2-8 (AN-032/AN-033): нормализация utm_source + детект AI-трафика.
# ---------------------------------------------------------------------------

# Канонические имена источников → все встречающиеся написания.
# Governance-конвенция: канон — lowercase, без пробелов; см. docs/UTM_GOVERNANCE.md.
UTM_SOURCE_ALIASES = {
    'instagram': [
        'ig', 'inst', 'insta', 'instagram', 'igshopping', 'ig_shopping',
        'inst_vid', 'instvid', 'ig_stories', 'instagram_stories',
        'instagram_reels', 'ig_reels', 'l.instagram.com',
    ],
    'facebook': [
        'fb', 'facebook', 'meta', 'fb_ads', 'facebook_ads', 'facebookads',
        'm.facebook.com', 'l.facebook.com', 'lm.facebook.com', 'facebook.com',
    ],
    'tiktok': ['tt', 'tiktok', 'tik_tok', 'tiktok_ads', 'tiktokads'],
    'google': ['google', 'adwords', 'google_ads', 'googleads', 'google.com'],
    'telegram': ['tg', 'telegram', 'org.telegram.messenger', 't.me'],
    'youtube': ['yt', 'youtube', 'youtube.com'],
    # AI-источники (детектятся и по referrer — см. detect_ai_source)
    'chatgpt': ['chatgpt', 'chatgpt.com', 'chat.openai.com', 'openai'],
    'perplexity': ['perplexity', 'perplexity.ai'],
    'gemini': ['gemini', 'gemini.google.com', 'bard'],
    'claude': ['claude', 'claude.ai'],
    'copilot': ['copilot', 'copilot.microsoft.com', 'bing_chat'],
    'you': ['you', 'you.com'],
    'poe': ['poe', 'poe.com'],
}

# Обратный индекс: alias (lowercase) → канон
_UTM_SOURCE_REVERSE = {}
for _canonical, _aliases in UTM_SOURCE_ALIASES.items():
    _UTM_SOURCE_REVERSE[_canonical] = _canonical
    for _alias in _aliases:
        _UTM_SOURCE_REVERSE[_alias] = _canonical

# Источники, относящиеся к каналу «AI» (utm_medium=ai)
AI_SOURCES = {
    'chatgpt', 'perplexity', 'gemini', 'claude', 'copilot', 'you', 'poe',
}

# Referrer-домен → канонический AI-источник (суффикс-матч по hostname)
AI_REFERRER_DOMAINS = {
    'chatgpt.com': 'chatgpt',
    'chat.openai.com': 'chatgpt',
    'perplexity.ai': 'perplexity',
    'gemini.google.com': 'gemini',
    'claude.ai': 'claude',
    'copilot.microsoft.com': 'copilot',
    'you.com': 'you',
    'poe.com': 'poe',
}

CLICK_ID_ATTRIBUTION = (
    ('fbclid', 'facebook', 'paid_social'),
    ('gclid', 'google', 'cpc'),
    ('ttclid', 'tiktok', 'paid_social'),
)


def infer_click_id_attribution(payload: Optional[dict]) -> tuple[Optional[str], Optional[str]]:
    """Infer canonical acquisition fields when a landing has only a click ID."""
    values = payload or {}
    for field, source, medium in CLICK_ID_ATTRIBUTION:
        if str(values.get(field) or '').strip():
            return source, medium
    return None, None


def normalize_utm_source(value: Optional[str]) -> Optional[str]:
    """
    Нормализует utm_source по словарю алиасов.

    'IG'/'Instagram'/'IGShopping'/'Inst_Vid' → 'instagram' и т.д.
    Неизвестные значения приводятся к lowercase (чтобы 'Newsletter' и
    'newsletter' не были двумя источниками), но не переименовываются.
    """
    if not value:
        return value
    cleaned = str(value).strip().lower()
    if not cleaned:
        return None
    return _UTM_SOURCE_REVERSE.get(cleaned, cleaned)


def detect_ai_source(referrer: Optional[str]) -> Optional[str]:
    """
    Определяет AI-источник по referrer-домену.

    Возвращает канонический источник ('chatgpt', 'perplexity', …) или None.
    Матч по суффиксу hostname, чтобы ловить www./m.-поддомены.
    """
    if not referrer:
        return None
    try:
        from urllib.parse import urlparse
        hostname = (urlparse(referrer).hostname or '').lower()
    except Exception:
        return None
    if not hostname:
        return None
    for domain, source in AI_REFERRER_DOMAINS.items():
        if hostname == domain or hostname.endswith('.' + domain):
            return source
    return None


def normalize_utm_attribution(
    source: Optional[str],
    medium: Optional[str],
    *,
    referrer: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Return one canonical source/medium pair for every attribution writer.

    The source can come from an explicit UTM value, a legacy first-touch
    cookie, or an AI referrer. Explicit non-empty media remain untouched; an
    AI source receives ``ai`` only when the medium is absent.
    """
    canonical_source = normalize_utm_source(source)
    if not canonical_source:
        canonical_source = detect_ai_source(referrer)

    clean_medium = str(medium).strip() if medium is not None else None
    if canonical_source in AI_SOURCES and not clean_medium:
        clean_medium = 'ai'

    return canonical_source, clean_medium or None


def normalize_first_touch_attribution(snapshot: Optional[dict]) -> dict:
    """Canonicalize a first-touch payload without dropping unrelated keys."""
    normalized = dict(snapshot or {})
    source, medium = normalize_utm_attribution(
        normalized.get('utm_source'),
        normalized.get('utm_medium'),
        referrer=normalized.get('referrer'),
    )
    if not source:
        source, medium = infer_click_id_attribution(normalized)
    if source:
        normalized['utm_source'] = source
    if medium:
        normalized['utm_medium'] = medium
    return normalized


def is_bot_user_agent(user_agent: str) -> bool:
    """
    Определяет, является ли User-Agent ботом.

    Args:
        user_agent: Строка User-Agent

    Returns:
        bool: True если бот, False если обычный пользователь
    """
    if not user_agent:
        return False

    ua_lower = user_agent.lower()

    bot_patterns = [
        'bot', 'crawler', 'spider', 'scraper',
        'google', 'facebook', 'twitter', 'linkedin',
        'curl', 'wget', 'python-requests',
        'headless', 'phantom', 'selenium',
    ]

    return any(pattern in ua_lower for pattern in bot_patterns)
