"""
Django REST Framework Serializers for Storefront API.

Сериализаторы для преобразования моделей в JSON и обратно.
Используются ViewSets для автоматической генерации API endpoints.
"""

from rest_framework import serializers
from .models import Product, Category
from productcolors.models import ProductColorVariant


class CategorySerializer(serializers.ModelSerializer):
    """
    Сериализатор для категорий товаров.

    Fields:
        - id: ID категории
        - name: Название категории
        - slug: URL slug
        - is_active: Активна ли категория
        - products_count: Количество товаров (read-only)
    """
    products_count = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ['id', 'name', 'slug', 'products_count']
        read_only_fields = ['id', 'slug']

    def get_products_count(self, obj) -> int:
        """Возвращает количество товаров в категории."""
        return obj.products.filter(status='published').count()


class ProductColorSerializer(serializers.ModelSerializer):
    """
    Сериализатор для цветовых вариантов товара.

    Fields:
        - id: ID варианта
        - color: Объект цвета
        - is_default: Дефолтный вариант
        - order: Порядок сортировки
    """
    class Meta:
        model = ProductColorVariant
        fields = ['id', 'color', 'is_default', 'order']
        depth = 1  # Включаем вложенный объект color


class ProductListSerializer(serializers.ModelSerializer):
    """
    Сериализатор для списка товаров (минимальная информация).

    Оптимизирован для быстрой загрузки списков.
    Fields:
        - id: ID товара
        - title: Название
        - slug: URL slug
        - retail_price: Розничная цена
        - image: Главное изображение (URL)
        - category: Название категории
        - is_active: Активен ли товар
        - in_stock: В наличии
    """
    category = serializers.CharField(source='category.name', read_only=True)
    image = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'title', 'slug', 'price', 'image',
            'category', 'featured'
        ]
        read_only_fields = ['id', 'slug']

    def get_image(self, obj) -> str | None:
        """Возвращает URL главного изображения."""
        if obj.main_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.main_image.url)
            return obj.main_image.url
        return None


class ProductDetailSerializer(serializers.ModelSerializer):
    """
    Сериализатор для детальной информации о товаре.

    Включает полную информацию о товаре и связанных сущностях.
    Fields:
        - id: ID товара
        - title: Название
        - slug: URL slug
        - description: Описание
        - retail_price: Розничная цена
        - wholesale_price: Оптовая цена
        - drop_price: Дропшиппинг цена
        - image: Главное изображение
        - category: Объект категории
        - is_active: Активен
        - in_stock: В наличии
        - colors: Список цветовых вариантов
        - points_reward: Баллы за покупку
    """
    category = CategorySerializer(read_only=True)
    colors = ProductColorSerializer(many=True, read_only=True, source='color_variants')
    image = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'title', 'slug', 'description',
            'price', 'wholesale_price', 'drop_price',
            'image', 'category', 'featured',
            'colors', 'points_reward'
        ]
        read_only_fields = ['id', 'slug']

    def get_image(self, obj) -> str | None:
        """Возвращает URL главного изображения."""
        if obj.main_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.main_image.url)
            return obj.main_image.url
        return None


class CartItemSerializer(serializers.Serializer):
    """
    Сериализатор для элемента корзины.

    Fields:
        - product_id: ID товара
        - quantity: Количество
        - color: Цвет (optional)
        - size: Размер (optional)
    """
    product_id = serializers.IntegerField(required=True)
    quantity = serializers.IntegerField(required=True, min_value=1)
    color = serializers.CharField(required=False, allow_blank=True)
    size = serializers.CharField(required=False, allow_blank=True)

    def validate_product_id(self, value):
        """Проверяет существование товара."""
        if not Product.objects.filter(id=value).exists():
            raise serializers.ValidationError("Товар не найден")
        return value


class SearchQuerySerializer(serializers.Serializer):
    """
    Сериализатор для параметров поиска.

    Fields:
        - q: Поисковый запрос
        - category: ID категории (optional)
        - min_price: Минимальная цена (optional)
        - max_price: Максимальная цена (optional)
        - in_stock: Только в наличии (optional)
    """
    q = serializers.CharField(required=True, max_length=200)
    category = serializers.IntegerField(required=False, allow_null=True)
    min_price = serializers.DecimalField(
        required=False,
        allow_null=True,
        max_digits=10,
        decimal_places=2
    )
    max_price = serializers.DecimalField(
        required=False,
        allow_null=True,
        max_digits=10,
        decimal_places=2
    )
    in_stock = serializers.BooleanField(required=False, default=False)


# ==================== AJAX ENDPOINTS SERIALIZERS ====================

class SearchSuggestionSerializer(serializers.Serializer):
    """
    Сериализатор для автодополнения поиска.

    Fields:
        - id: ID товара
        - title: Название товара
        - slug: URL slug
    """
    id = serializers.IntegerField(read_only=True)
    title = serializers.CharField(read_only=True)
    slug = serializers.SlugField(read_only=True)


class ProductAvailabilitySerializer(serializers.Serializer):
    """
    Сериализатор для проверки доступности товара.

    Fields:
        - available: Доступен ли товар
        - in_stock: В наличии
        - message: Сообщение о доступности
    """
    available = serializers.BooleanField(read_only=True)
    in_stock = serializers.BooleanField(read_only=True)
    message = serializers.CharField(read_only=True)


class RelatedProductSerializer(serializers.ModelSerializer):
    """
    Упрощенный сериализатор для похожих товаров.

    Fields:
        - id: ID товара
        - title: Название
        - slug: URL slug
        - price: Цена
        - final_price: Итоговая цена (с учетом скидки)
        - main_image: URL изображения
    """
    main_image = serializers.SerializerMethodField()
    final_price = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ['id', 'title', 'slug', 'price', 'final_price', 'main_image']
        read_only_fields = ['id', 'slug']

    def get_main_image(self, obj):
        """Возвращает URL главного изображения."""
        if obj.main_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.main_image.url)
            return obj.main_image.url
        return None

    def get_final_price(self, obj):
        """Возвращает итоговую цену с учетом скидки."""
        if hasattr(obj, 'final_price'):
            return obj.final_price
        if obj.has_discount and obj.discount_percent:
            return int(obj.price * (1 - obj.discount_percent / 100))
        return obj.price


class TrackEventSerializer(serializers.Serializer):
    """
    Сериализатор для трекинга событий аналитики.

    Fields:
        - event_type: Тип события (view, click, add_to_cart, etc.)
        - product_id: ID товара (optional)
        - category_id: ID категории (optional)
        - metadata: Дополнительные данные (JSON, optional)
    """
    event_type = serializers.CharField(required=True, max_length=100)
    product_id = serializers.IntegerField(required=False, allow_null=True)
    category_id = serializers.IntegerField(required=False, allow_null=True)
    metadata = serializers.JSONField(required=False, default=dict)

    def validate_event_type(self, value):
        """Проверяет валидность типа события."""
        allowed_types = [
            'view', 'click', 'add_to_cart', 'remove_from_cart',
            'purchase', 'search', 'favorite', 'share'
        ]
        if value not in allowed_types:
            raise serializers.ValidationError(
                f"Недопустимый тип события. Разрешены: {', '.join(allowed_types)}"
            )
        return value


class NewsletterSubscribeSerializer(serializers.Serializer):
    """
    Сериализатор для подписки на рассылку.

    Fields:
        - email: Email адрес подписчика
    """
    email = serializers.EmailField(required=True)

    def validate_email(self, value):
        """Проверяет валидность email."""
        if not value or len(value) < 5:
            raise serializers.ValidationError("Введите корректный email адрес")
        return value.lower().strip()


class ContactFormSerializer(serializers.Serializer):
    """
    Сериализатор для формы обратной связи.

    Fields:
        - name: Имя отправителя
        - email: Email отправителя
        - phone: Телефон (optional)
        - message: Текст сообщения
    """
    name = serializers.CharField(required=True, max_length=100)
    email = serializers.EmailField(required=True)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=20)
    message = serializers.CharField(required=True, max_length=1000)

    def validate_name(self, value):
        """Проверяет имя."""
        if len(value.strip()) < 2:
            raise serializers.ValidationError("Ім'я занадто коротке")
        return value.strip()

    def validate_message(self, value):
        """Проверяет сообщение."""
        if len(value.strip()) < 10:
            raise serializers.ValidationError("Повідомлення занадто коротке")
        return value.strip()
