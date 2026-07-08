"""
TikTok Events API Service

Сервис для отправки серверных событий (Purchase, Lead) в TikTok Events API.
Используется для автоматической отправки событий при изменении статуса заказа.

Документация TikTok Events API:
https://ads.tiktok.com/help/article?aid=10002887
"""

import hashlib
import logging
from typing import Optional, Dict, Any, List

import requests
from django.conf import settings
from django.utils import timezone

from storefront.utils.analytics_helpers import get_offer_id as build_offer_id

logger = logging.getLogger(__name__)


class TikTokEventsService:
    """Сервис для работы с TikTok Events API (server-to-server)."""

    # W2-6 (AN-020): миграция с legacy `v1.3/pixel/track/` на Events API 2.0
    # (`v1.3/event/track/`) — legacy-эндпоинт deprecated и не поддерживает
    # стандартные имена событий в отчётах целей.
    DEFAULT_ENDPOINT = 'https://business-api.tiktok.com/open_api/v1.3/event/track/'

    # W2-6 (AN-020): маппинг Meta-имён → стандартные имена TikTok.
    # TikTok-цели (оптимизация кампаний) видят ТОЛЬКО стандартные имена:
    # Purchase → CompletePayment (полная оплата),
    # Lead → PlaceAnOrder (предоплаченный заказ / заявка на заказ).
    # event_id при этом НЕ меняется — дедуп клиент/сервер сохраняется,
    # т.к. клиентский слой (analytics-loader.js) маппит имена так же.
    EVENT_NAME_MAP = {
        'Purchase': 'CompletePayment',
        'Lead': 'PlaceAnOrder',
    }

    def __init__(self) -> None:
        """Инициализация сервиса с настройками из ENV."""
        self.access_token = getattr(settings, 'TIKTOK_EVENTS_ACCESS_TOKEN', None)
        self.pixel_code = getattr(settings, 'TIKTOK_EVENTS_PIXEL_CODE', None)
        self.test_event_code = getattr(settings, 'TIKTOK_EVENTS_TEST_EVENT_CODE', None)
        self.api_endpoint = getattr(settings, 'TIKTOK_EVENTS_API_ENDPOINT', self.DEFAULT_ENDPOINT)

        if not self.access_token or not self.pixel_code:
            logger.error(
                "❌ TikTok Events API не настроен! "
                "Необходимо установить TIKTOK_EVENTS_ACCESS_TOKEN и TIKTOK_EVENTS_PIXEL_CODE в ENV. "
                f"Access Token: {'установлен' if self.access_token else 'НЕ установлен'}, "
                f"Pixel Code: {'установлен' if self.pixel_code else 'НЕ установлен'}"
            )
            self.enabled = False
            self.session: Optional[requests.Session] = None
            return

        self.enabled = True
        self.session = requests.Session()
        self.session.headers.update({
            'Access-Token': self.access_token,
            'Content-Type': 'application/json',
        })
        logger.info(
            f"✅ TikTok Events API client initialized successfully: "
            f"Pixel Code={self.pixel_code[:10]}..."
        )

    @staticmethod
    def _hash_value(value: Optional[str]) -> Optional[str]:
        """Хеширует строковое значение (SHA-256) для передачи в TikTok."""
        if not value:
            return None

        cleaned = str(value).strip().lower()
        if not cleaned:
            return None

        return hashlib.sha256(cleaned.encode('utf-8')).hexdigest()

    @staticmethod
    def _normalize_phone(phone: Optional[str]) -> Optional[str]:
        """Оставляет в телефоне только цифры."""
        if not phone:
            return None
        digits = ''.join(ch for ch in str(phone) if ch.isdigit())
        return digits or None

    def _build_user_context(self, order) -> Dict[str, Any]:
        """Формирует блок user для Events API."""
        user_context: Dict[str, Any] = {}

        # Email
        if order.user and order.user.email:
            hashed_email = self._hash_value(order.user.email)
            if hashed_email:
                user_context['email'] = [hashed_email]

        # Phone
        normalized_phone = self._normalize_phone(order.phone)
        if normalized_phone:
            hashed_phone = self._hash_value(normalized_phone)
            if hashed_phone:
                user_context['phone'] = [hashed_phone]

        # External ID (используем ID пользователя или номер заказа)
        external_source = None
        if order.user_id:
            external_source = str(order.user_id)
        elif order.order_number:
            external_source = order.order_number

        if external_source:
            hashed_external = self._hash_value(external_source)
            if hashed_external:
                user_context['external_id'] = [hashed_external]

        # City как дополнительный идентификатор
        if order.city:
            hashed_city = self._hash_value(order.city)
            if hashed_city:
                user_context['city'] = [hashed_city]

        tracking_data: Dict[str, Any] = {}
        if order.payment_payload and isinstance(order.payment_payload, dict):
            tracking_data = order.payment_payload.get('tracking') or {}

        ttclid = tracking_data.get('ttclid') if isinstance(tracking_data, dict) else None
        if ttclid:
            user_context['ttclid'] = ttclid

        # Данные из payment_payload (IP, User-Agent)
        if order.payment_payload and isinstance(order.payment_payload, dict):
            ip_address = order.payment_payload.get('client_ip_address')
            user_agent = order.payment_payload.get('client_user_agent')
            if ip_address:
                user_context['ip'] = ip_address
            if user_agent:
                user_context['user_agent'] = user_agent

        return user_context

    def _build_contents(self, order) -> List[Dict[str, Any]]:
        """Формирует список contents для TikTok."""
        contents: List[Dict[str, Any]] = []

        items = order.items.select_related('product', 'product__category', 'color_variant').all()
        for item in items:
            color_variant_id = item.color_variant.id if item.color_variant else None
            size = (item.size or 'S').upper()
            getter = getattr(item.product, "get_offer_id", None)
            if callable(getter):
                offer_id = getter(color_variant_id, size)
            elif item.product_id:
                offer_id = build_offer_id(item.product.id, color_variant_id, size)
            else:
                # Позиція поза каталогом — стабільний offer_id за номером позиції.
                offer_id = f"manual-{item.pk}"

            content: Dict[str, Any] = {
                'content_id': offer_id,
                'content_type': 'product',
                'content_name': item.title,
                'quantity': int(item.qty),
                'price': float(item.unit_price),
                'brand': 'TwoComms',
            }

            product_category = getattr(item.product, 'category', None)
            if product_category and getattr(product_category, 'name', None):
                content['content_category'] = product_category.name

            contents.append(content)

        return contents

    def _build_properties(self, order, event_name: str = 'Purchase') -> Dict[str, Any]:
        """Формирует properties с деталями заказа."""
        include_items = event_name != 'Lead'
        contents: List[Dict[str, Any]] = self._build_contents(order) if include_items else []

        if event_name == 'Lead' and order.payment_status == 'prepaid':
            prepayment_amount = order.get_prepayment_amount()
            total_value = float(prepayment_amount or 0)
            if total_value <= 0:
                total_value = 200.0  # стандартная сумма предоплаты
        else:
            total_value = float(order.total_sum)

        properties: Dict[str, Any] = {
            'value': total_value,
            'currency': 'UAH',
            'description': 'web_checkout',
        }

        if include_items and contents:
            properties['contents'] = contents
            properties['num_items'] = sum(content.get('quantity', 0) for content in contents)
        else:
            properties['num_items'] = sum(content.get('quantity', 0) for content in contents) if contents else 0

        if order.order_number:
            properties['order_id'] = order.order_number

        if order.payment_status:
            properties['status'] = order.payment_status

        if order.pay_type:
            properties['payment_type'] = order.pay_type

        return properties

    def _build_payload(
        self,
        order,
        event_name: str,
        event_id: str,
        source_url: Optional[str],
        test_event_code: Optional[str],
    ) -> Dict[str, Any]:
        """
        Собирает payload для TikTok Events API 2.0 (`/event/track/`).

        W2-6: структура 2.0 — `event_source`/`event_source_id` + массив
        `data[]`; имя события маппится на стандартное TikTok-имя
        (Purchase→CompletePayment, Lead→PlaceAnOrder), event_id сохраняется
        как есть — клиентский пиксель шлёт тот же id → дедуп работает.
        """
        tiktok_event_name = self.EVENT_NAME_MAP.get(event_name, event_name)

        event_time = order.created or timezone.now()

        event_data: Dict[str, Any] = {
            'event': tiktok_event_name,
            'event_time': int(event_time.timestamp()),
            'event_id': event_id,
            'page': {
                'url': source_url or f"https://twocomms.shop/orders/{order.order_number}/"
            },
            'properties': self._build_properties(order, event_name=event_name),
        }

        user_context = self._build_user_context(order)
        if user_context:
            event_data['user'] = user_context

        payload: Dict[str, Any] = {
            'event_source': 'web',
            'event_source_id': self.pixel_code,
            'data': [event_data],
        }

        # Добавляем test_event_code для тестирования Events API
        code = test_event_code or self.test_event_code
        if code:
            payload['test_event_code'] = code

        return payload

    def _send_payload(self, payload: Dict[str, Any]) -> bool:
        """Отправляет подготовленный payload в TikTok Events API."""
        if not self.enabled or not self.session:
            logger.debug("TikTok Events API disabled, skipping payload: %s", payload)
            return False

        try:
            response = self.session.post(self.api_endpoint, json=payload, timeout=15)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.error("❌ TikTok Events API request failed: %s", exc, exc_info=True)
            return False

        try:
            data = response.json()
        except ValueError:
            logger.error("❌ TikTok Events API returned non-JSON response: %s", response.text)
            return False

        if data.get('code') == 0:
            events = payload.get('data') or [{}]
            logger.info(
                "✅ TikTok Events API accepted event: %s (%s)",
                events[0].get('event_id'),
                events[0].get('event'),
            )
            return True

        logger.error("❌ TikTok Events API responded with error: %s", data)
        return False

    def send_event(
        self,
        order,
        event_name: str,
        event_id: str,
        source_url: Optional[str] = None,
        test_event_code: Optional[str] = None,
    ) -> bool:
        """Унифицированная отправка события в TikTok Events API."""
        if not self.enabled:
            logger.warning("TikTok Events API disabled, skipping event %s", event_name)
            return False

        payload = self._build_payload(order, event_name, event_id, source_url, test_event_code)
        return self._send_payload(payload)

    def send_purchase_event(
        self,
        order,
        source_url: Optional[str] = None,
        test_event_code: Optional[str] = None,
    ) -> bool:
        """Отправляет Purchase событие (полная оплата)."""
        event_id = order.get_purchase_event_id()
        return self.send_event(order, 'Purchase', event_id, source_url, test_event_code)

    def send_lead_event(
        self,
        order,
        source_url: Optional[str] = None,
        test_event_code: Optional[str] = None,
    ) -> bool:
        """Отправляет Lead событие (предоплата/заявка)."""
        event_id = order.get_lead_event_id()
        return self.send_event(order, 'Lead', event_id, source_url, test_event_code)


_tiktok_service: Optional[TikTokEventsService] = None


def get_tiktok_events_service() -> TikTokEventsService:
    """Возвращает глобальный экземпляр TikTok Events Service (Singleton)."""
    global _tiktok_service
    if _tiktok_service is None:
        _tiktok_service = TikTokEventsService()
    return _tiktok_service
