"""
Система рекомендаций товаров для увеличения конверсии
"""
from django.db.models import Count
from .models import Product, ProductStatus
from accounts.models import FavoriteProduct
from orders.models import Order, OrderItem
from cache_utils import get_cache


class ProductRecommendationEngine:
    """
    Движок рекомендаций товаров
    """

    def __init__(self, user=None, cache_alias='fragments'):
        self.user = user
        self.cache_timeout = 300  # 5 минут
        self.cache = get_cache(cache_alias)

    def get_recommendations(self, product=None, limit=8):
        """
        Получает рекомендации товаров
        """
        try:
            from .services.catalog_helpers import get_public_product_order_version
            public_version = get_public_product_order_version()
        except Exception:
            public_version = '0'
        cache_key = (
            f"recommendations_{self.user.id if self.user else 'anon'}_"
            f"{product.id if product else 'home'}_limit_{limit}_public_{public_version}"
        )
        recommendations = self.cache.get(cache_key)

        if recommendations is None:
            if product:
                recommendations = self._get_product_recommendations(product, limit)
            else:
                recommendations = self._get_home_recommendations(limit)

            self.cache.set(cache_key, recommendations, self.cache_timeout)

        return recommendations

    def _get_product_recommendations(self, product, limit):
        """
        Рекомендации на основе конкретного товара
        """
        recommendations = []

        # 1. Товары из той же категории
        category_products = Product.objects.filter(
            category=product.category,
            status=ProductStatus.PUBLISHED
        ).exclude(id=product.id)[:limit//2]

        recommendations.extend(category_products)

        # 2. Товары, которые часто покупают вместе
        if len(recommendations) < limit:
            frequently_bought_together = self._get_frequently_bought_together(product, limit - len(recommendations))
            recommendations.extend(frequently_bought_together)

        # 3. Популярные товары, если не хватает
        if len(recommendations) < limit:
            popular_products = self._get_popular_products(limit - len(recommendations))
            recommendations.extend(popular_products)

        return recommendations[:limit]

    def _get_home_recommendations(self, limit):
        """
        Рекомендации для главной страницы
        """
        recommendations = []

        # 1. Рекомендации на основе истории пользователя
        if self.user and self.user.is_authenticated:
            user_recommendations = self._get_user_based_recommendations(limit//2)
            recommendations.extend(user_recommendations)

        # 2. Популярные товары
        if len(recommendations) < limit:
            popular_products = self._get_popular_products(limit - len(recommendations))
            recommendations.extend(popular_products)

        # 3. Новые товары
        if len(recommendations) < limit:
            new_products = Product.objects.filter(
                status=ProductStatus.PUBLISHED
            ).order_by('-id')[:limit - len(recommendations)]
            recommendations.extend(new_products)

        return recommendations[:limit]

    def _get_frequently_bought_together(self, product, limit):
        """
        Товары, которые часто покупают вместе с данным
        """
        # Получаем заказы, содержащие данный товар
        orders_with_product = OrderItem.objects.filter(
            product=product
        ).values_list('order_id', flat=True)

        # Получаем другие товары из этих заказов
        related_products = OrderItem.objects.filter(
            order_id__in=orders_with_product
        ).exclude(product=product).values('product').annotate(
            count=Count('product')
        ).order_by('-count')[:limit]

        product_ids = [item['product'] for item in related_products]
        return Product.objects.filter(
            id__in=product_ids,
            status=ProductStatus.PUBLISHED
        )[:limit]

    def _get_popular_products(self, limit):
        """
        Популярные товары на основе количества заказов
        """
        popular_products = Product.objects.filter(
            status=ProductStatus.PUBLISHED
        ).annotate(
            order_count=Count('orderitem')
        ).order_by('-order_count', '-id')[:limit]

        return popular_products

    def _get_user_based_recommendations(self, limit):
        """
        Рекомендации на основе предпочтений пользователя
        """
        if not self.user or not self.user.is_authenticated:
            return []

        recommendations = []

        # 1. Товары из избранного
        favorite_categories = FavoriteProduct.objects.filter(
            user=self.user
        ).values_list('product__category', flat=True).distinct()

        if favorite_categories:
            category_products = Product.objects.filter(
                category__in=favorite_categories,
                status=ProductStatus.PUBLISHED
            ).exclude(
                id__in=FavoriteProduct.objects.filter(user=self.user).values_list('product_id', flat=True)
            )[:limit//2]
            recommendations.extend(category_products)

        # 2. Товары из истории заказов
        if len(recommendations) < limit:
            user_orders = Order.objects.filter(user=self.user)
            ordered_categories = OrderItem.objects.filter(
                order__in=user_orders
            ).values_list('product__category', flat=True).distinct()

            if ordered_categories:
                history_products = Product.objects.filter(
                    category__in=ordered_categories,
                    status=ProductStatus.PUBLISHED
                ).exclude(
                    id__in=OrderItem.objects.filter(order__in=user_orders).values_list('product_id', flat=True)
                )[:limit - len(recommendations)]
                recommendations.extend(history_products)

        return recommendations[:limit]

    def get_trending_products(self, limit=6):
        """
        Трендовые товары (популярные за последние 7 дней)
        """
        from datetime import timedelta
        from django.utils import timezone

        cache_key = "trending_products"
        trending = self.cache.get(cache_key)

        if trending is None:
            week_ago = timezone.now() - timedelta(days=7)

            trending = Product.objects.filter(
                status=ProductStatus.PUBLISHED,
                orderitem__order__created__gte=week_ago
            ).annotate(
                recent_orders=Count('orderitem')
            ).order_by('-recent_orders', '-id')[:limit]

            self.cache.set(cache_key, list(trending), 600)  # 10 минут

        return trending

    def get_seasonal_recommendations(self, limit=6):
        """
        Сезонные рекомендации
        """
        from django.utils import timezone

        current_month = timezone.localdate().month

        # Определяем сезон
        if current_month in [12, 1, 2]:
            season = 'winter'
        elif current_month in [3, 4, 5]:
            season = 'spring'
        elif current_month in [6, 7, 8]:
            season = 'summer'
        else:
            season = 'autumn'

        cache_key = f"seasonal_products_{season}"
        seasonal = self.cache.get(cache_key)

        if seasonal is None:
            # Здесь можно добавить логику для сезонных товаров
            # Пока возвращаем популярные товары
            seasonal = self._get_popular_products(limit)
            self.cache.set(cache_key, list(seasonal), 3600)  # 1 час

        return seasonal
