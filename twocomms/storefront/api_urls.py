"""
Django REST Framework API URLs with Router.

Автоматически генерирует URL patterns для ViewSets.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .viewsets import (
    CategoryViewSet,
    ProductViewSet,
    CartViewSet,
    AnalyticsViewSet,
    CommunicationViewSet,
    AdminProductBuilderViewSet,
)
from .admin_analytics_api import AdminAnalyticsViewSet
from .utm_api_views import UTMAnalyticsViewSet, export_utm_csv


# Создаем Router
router = DefaultRouter()

# Регистрируем ViewSets
router.register(r'categories', CategoryViewSet, basename='api-category')
router.register(r'products', ProductViewSet, basename='api-product')
router.register(r'cart', CartViewSet, basename='api-cart')
# W3-5 (TD-024): /api/analytics/track/ снят с маршрутизации — публичный
# no-op (только logger.info, ничего не сохранял), ни один клиентский код
# его не вызывал; оставался лишь вектором лог-флуда. Реальный трекинг
# идёт через /api/track-event/ (storefront.views.api.track_event).
# router.register(r'analytics', AnalyticsViewSet, basename='api-analytics')
router.register(r'communication', CommunicationViewSet, basename='api-communication')
router.register(r'admin/product-builder', AdminProductBuilderViewSet, basename='api-admin-product-builder')
router.register(r'admin/analytics', AdminAnalyticsViewSet, basename='api-admin-analytics')
router.register(r'utm', UTMAnalyticsViewSet, basename='api-utm')

# URL patterns
urlpatterns = [
    path('', include(router.urls)),
    # Экспорт UTM данных в CSV
    path('utm/export-csv/', export_utm_csv, name='api-utm-export-csv'),
]

# Автоматически созданные URLs:
# GET    /api/categories/                    - Список категорий
# GET    /api/categories/{slug}/             - Детали категории
# GET    /api/products/                      - Список товаров
# GET    /api/products/{slug}/               - Детали товара
# GET    /api/products/search/               - Поиск товаров
# GET    /api/products/by-category/{slug}/   - Товары по категории
# GET    /api/cart/                          - Содержимое корзины
# POST   /api/cart/add/                      - Добавить в корзину
# POST   /api/cart/remove/                   - Удалить из корзины
# POST   /api/cart/clear/                    - Очистить корзину
