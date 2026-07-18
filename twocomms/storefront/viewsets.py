"""
Django REST Framework ViewSets for Storefront API.

ViewSets обеспечивают CRUD операции и кастомные endpoints для API.
Используют сериализаторы для преобразования данных в JSON.
"""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import (
    AllowAny,
    IsAuthenticatedOrReadOnly,
    IsAuthenticated,
    IsAdminUser,
)
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from django.db.models import Q
from django.shortcuts import get_object_or_404

from .models import Product, Category, Catalog
from .serializers import (
    ProductListSerializer,
    ProductDetailSerializer,
    CategorySerializer,
    CartItemSerializer,
    SearchQuerySerializer,
    # AJAX endpoints serializers
    SearchSuggestionSerializer,
    ProductAvailabilitySerializer,
    RelatedProductSerializer,
    TrackEventSerializer,
    NewsletterSubscribeSerializer,
    ContactFormSerializer
)
from .services.product_builder import (
    get_product_builder_payload,
    serialize_catalog,
    list_catalog_payloads,
)


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet для категорий товаров.

    Предоставляет:
        - list: GET /api/categories/ - список всех категорий
        - retrieve: GET /api/categories/{id}/ - детали категории

    Permissions: Read-only для всех пользователей
    """
    queryset = Category.objects.filter(is_active=True).order_by('name')
    serializer_class = CategorySerializer
    permission_classes = [AllowAny]
    lookup_field = 'slug'


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet для товаров.

    Предоставляет:
        - list: GET /api/products/ - список товаров
        - retrieve: GET /api/products/{id}/ - детали товара
        - search: GET /api/products/search/?q=query - поиск товаров
        - by_category: GET /api/products/by_category/{slug}/ - товары по категории

    Permissions: Read-only для всех пользователей
    Pagination: По умолчанию 20 товаров на страницу
    """
    permission_classes = [AllowAny]
    lookup_field = 'slug'

    def get_queryset(self):
        """
        Возвращает queryset товаров с оптимизацией.

        Использует select_related для минимизации запросов к БД.
        """
        return Product.objects.filter(
            status='published',
            category__is_active=True,
        ).select_related('category').order_by('-id')

    def get_serializer_class(self):
        """
        Возвращает сериализатор в зависимости от action.

        - list: ProductListSerializer (минимальная информация)
        - retrieve: ProductDetailSerializer (полная информация)
        """
        if self.action == 'retrieve':
            return ProductDetailSerializer
        return ProductListSerializer

    @action(detail=False, methods=['get'], url_path='search')
    def search(self, request):
        """
        Поиск товаров по запросу.

        Query Parameters:
            - q: Поисковый запрос (required)
            - category: ID категории (optional)
            - min_price: Минимальная цена (optional)
            - max_price: Максимальная цена (optional)
            - in_stock: Только в наличии (optional)

        Returns:
            - 200: Список найденных товаров
            - 400: Ошибка валидации параметров

        Example:
            GET /api/products/search/?q=футболка&category=1&in_stock=true
        """
        # Валидация параметров
        serializer = SearchQuerySerializer(data=request.query_params)
        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )

        validated_data = serializer.validated_data
        query = validated_data.get('q', '')

        # Базовый queryset
        queryset = self.get_queryset()

        # Поиск по названию и описанию
        if query:
            queryset = queryset.filter(
                Q(title__icontains=query) |
                Q(description__icontains=query)
            )

        # Фильтр по категории
        if validated_data.get('category'):
            queryset = queryset.filter(category_id=validated_data['category'])

        # Фильтр по цене
        if validated_data.get('min_price'):
            queryset = queryset.filter(price__gte=validated_data['min_price'])
        if validated_data.get('max_price'):
            queryset = queryset.filter(price__lte=validated_data['max_price'])

        # Фильтр "только в наличии" - пропускаем, т.к. поле отсутствует в модели
        # if validated_data.get('in_stock'):
        #     queryset = queryset.filter(in_stock=True)

        # Пагинация
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = ProductListSerializer(
                page,
                many=True,
                context={'request': request}
            )
            return self.get_paginated_response(serializer.data)

        serializer = ProductListSerializer(
            queryset,
            many=True,
            context={'request': request}
        )
        return Response(serializer.data)

    @extend_schema(
        parameters=[
            OpenApiParameter("category_slug", OpenApiTypes.STR, OpenApiParameter.PATH),
        ],
    )
    @action(detail=False, methods=['get'], url_path='by-category/(?P<category_slug>[^/.]+)')
    def by_category(self, request, category_slug=None):
        """
        Получить товары по slug категории.

        Args:
            category_slug: URL slug категории

        Returns:
            - 200: Список товаров категории
            - 404: Категория не найдена

        Example:
            GET /api/products/by-category/odezhda/
        """
        try:
            category = Category.objects.get(slug=category_slug, is_active=True)
        except Category.DoesNotExist:
            return Response(
                {'error': 'Категория не найдена'},
                status=status.HTTP_404_NOT_FOUND
            )

        queryset = self.get_queryset().filter(category=category)

        # Пагинация
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = ProductListSerializer(
                page,
                many=True,
                context={'request': request}
            )
            return self.get_paginated_response(serializer.data)

        serializer = ProductListSerializer(
            queryset,
            many=True,
            context={'request': request}
        )
        return Response(serializer.data)

    @action(detail=True, methods=['get'], url_path='related')
    def related(self, request, slug=None):
        """
        Получить похожие товары.

        Возвращает товары из той же категории (кроме текущего).

        Args:
            slug: URL slug товара

        Returns:
            - 200: Список похожих товаров (до 6 штук)
            - 404: Товар не найден

        Example:
            GET /api/products/futbolka-classic/related/
        """
        product = self.get_object()

        # Ищем товары из той же категории
        related_products = self.get_queryset().filter(
            category=product.category
        ).exclude(
            id=product.id
        ).select_related('category')[:6]

        serializer = RelatedProductSerializer(
            related_products,
            many=True,
            context={'request': request}
        )

        return Response({
            'success': True,
            'products': serializer.data,
            'count': len(serializer.data)
        })

    @action(detail=True, methods=['get'], url_path='availability')
    def availability(self, request, slug=None):
        """
        Проверить доступность товара.

        Args:
            slug: URL slug товара

        Returns:
            - 200: Информация о доступности
            - 404: Товар не найден

        Example:
            GET /api/products/futbolka-classic/availability/
        """
        product = self.get_object()

        # TODO: Добавить реальную проверку наличия на складе
        # Пока просто возвращаем True для всех товаров

        data = {
            'available': True,
            'in_stock': True,
            'message': 'Товар доступний'
        }

        serializer = ProductAvailabilitySerializer(data)

        return Response({
            'success': True,
            **serializer.data
        })

    @action(detail=False, methods=['get'], url_path='suggestions')
    def suggestions(self, request):
        """
        Автодополнение для поиска товаров.

        Query Parameters:
            - q: Поисковый запрос (required, min 2 символа)
            - limit: Количество результатов (optional, default 5, max 10)

        Returns:
            - 200: Список предложений

        Example:
            GET /api/products/suggestions/?q=футб&limit=5
        """
        query = request.query_params.get('q', '').strip()
        try:
            requested_limit = int(request.query_params.get('limit', 5))
        except (TypeError, ValueError):
            requested_limit = 5
        limit = max(1, min(requested_limit, 10))

        if not query or len(query) < 2:
            return Response({
                'success': True,
                'suggestions': [],
                'count': 0
            })

        # Ищем по началу названия (быстрее чем contains)
        products = self.get_queryset().filter(
            title__istartswith=query
        ).values('id', 'title', 'slug')[:limit]

        serializer = SearchSuggestionSerializer(products, many=True)

        return Response({
            'success': True,
            'suggestions': serializer.data,
            'count': len(serializer.data)
        })


@extend_schema(responses=OpenApiTypes.OBJECT)
class AdminProductBuilderViewSet(viewsets.ViewSet):
    """
    Admin endpoints that power the product builder UI.

    Provides read-only payloads for now; write operations
    are handled via the HTML form and will be exposed in later phases.
    """

    permission_classes = [IsAuthenticated, IsAdminUser]

    @extend_schema(operation_id="admin_product_builder_list")
    def list(self, request):
        """Return base payload with active catalogs and their options."""
        payload = {"catalogs": list_catalog_payloads(active_only=True)}
        return Response(payload)

    @extend_schema(
        operation_id="admin_product_builder_retrieve",
        parameters=[OpenApiParameter("id", OpenApiTypes.INT, OpenApiParameter.PATH)],
    )
    def retrieve(self, request, pk=None):
        """Return product-centric payload for the builder."""
        product = get_object_or_404(
            Product.objects.select_related("catalog", "size_grid", "category"),
            pk=pk,
        )
        payload = get_product_builder_payload(product=product)
        return Response(payload)

    @extend_schema(operation_id="admin_product_builder_catalogs")
    @action(detail=False, methods=['get'], url_path='catalogs')
    def catalogs(self, request):
        """Explicit endpoint for fetching catalogs (supports ?active=false)."""
        active = request.query_params.get("active")
        active_only = True if active is None else active.lower() != "false"
        return Response({"catalogs": list_catalog_payloads(active_only=active_only)})

    @extend_schema(
        operation_id="admin_product_builder_catalog_detail",
        parameters=[OpenApiParameter("catalog_id", OpenApiTypes.INT, OpenApiParameter.PATH)],
    )
    @action(detail=False, methods=['get'], url_path='catalogs/(?P<catalog_id>\\d+)')
    def catalog_detail(self, request, catalog_id=None):
        """Fetch a single catalog with its options and size grids."""
        catalog = get_object_or_404(
            Catalog.objects.prefetch_related(
                "options__values",
                "size_grids",
            ),
            pk=catalog_id,
        )
        return Response({"catalog": serialize_catalog(catalog)})

    @extend_schema(operation_id="admin_product_builder_new_product")
    @action(detail=False, methods=['get'], url_path='product/new')
    def new_product(self, request):
        """
        Provide initial payload for a new product.

        Supports ?catalog=<id> to pre-select catalog options.
        """
        catalog_id = request.query_params.get("catalog")
        catalog = None
        if catalog_id:
            catalog = get_object_or_404(
                Catalog.objects.prefetch_related("options__values", "size_grids"),
                pk=catalog_id,
            )
        payload = get_product_builder_payload(catalog=catalog)
        return Response(payload)


@extend_schema(responses=OpenApiTypes.OBJECT)
class CartViewSet(viewsets.ViewSet):
    """
    ViewSet для операций с корзиной.

    Предоставляет:
        - add: POST /api/cart/add/ - добавить товар в корзину
        - remove: POST /api/cart/remove/ - удалить товар
        - clear: POST /api/cart/clear/ - очистить корзину
        - contents: GET /api/cart/ - содержимое корзины

    Permissions: Read-only для всех, write для аутентифицированных
    """
    permission_classes = [IsAuthenticatedOrReadOnly]

    @extend_schema(request=CartItemSerializer)
    @action(detail=False, methods=['post'])
    def add(self, request):
        """
        Добавить товар в корзину.

        Request Body:
            - product_id: ID товара (required)
            - quantity: Количество (required, >= 1)
            - color: Цвет (optional)
            - size: Размер (optional)

        Returns:
            - 200: Товар добавлен успешно
            - 400: Ошибка валидации
            - 404: Товар не найден

        Example:
            POST /api/cart/add/
            {
                "product_id": 123,
                "quantity": 2,
                "color": "Black",
                "size": "M"
            }
        """
        serializer = CartItemSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )

        validated_data = serializer.validated_data
        product_id = validated_data['product_id']
        quantity = validated_data['quantity']
        color = validated_data.get('color', '')
        size = validated_data.get('size', '')

        try:
            product = Product.objects.get(id=product_id)
        except Product.DoesNotExist:
            return Response(
                {'error': 'Товар не найден или недоступен'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Работа с сессией корзины
        cart = request.session.get('cart', {})
        cart_key = str(product_id)

        if cart_key in cart:
            cart[cart_key]['quantity'] += quantity
        else:
            cart[cart_key] = {
                'product_id': product_id,
                'quantity': quantity,
                'color': color,
                'size': size,
                'title': product.title,
                'price': float(product.price)
            }

        request.session['cart'] = cart
        request.session.modified = True

        return Response({
            'success': True,
            'message': f'Товар "{product.title}" додано до кошику',
            'cart_count': sum(item['quantity'] for item in cart.values())
        })

    @extend_schema(request=OpenApiTypes.OBJECT)
    @action(detail=False, methods=['post'])
    def remove(self, request):
        """
        Удалить товар из корзины.

        Request Body:
            - product_id: ID товара

        Returns:
            - 200: Товар удален
            - 404: Товар не найден в корзине
        """
        product_id = request.data.get('product_id')
        if not product_id:
            return Response(
                {'error': 'product_id обязателен'},
                status=status.HTTP_400_BAD_REQUEST
            )

        cart = request.session.get('cart', {})
        cart_key = str(product_id)

        if cart_key in cart:
            del cart[cart_key]
            request.session['cart'] = cart
            request.session.modified = True

            return Response({
                'success': True,
                'message': 'Товар видалено з кошику',
                'cart_count': sum(item['quantity'] for item in cart.values())
            })

        return Response(
            {'error': 'Товар не знайдено у кошику'},
            status=status.HTTP_404_NOT_FOUND
        )

    @extend_schema(request=None)
    @action(detail=False, methods=['post'])
    def clear(self, request):
        """
        Очистить корзину.

        Returns:
            - 200: Корзина очищена
        """
        request.session['cart'] = {}
        request.session.modified = True

        return Response({
            'success': True,
            'message': 'Кошик очищено',
            'cart_count': 0
        })

    def list(self, request):
        """
        Получить содержимое корзины.

        Returns:
            - 200: Список товаров в корзине
        """
        cart = request.session.get('cart', {})

        return Response({
            'cart': cart,
            'cart_count': sum(item['quantity'] for item in cart.values()),
            'total': sum(
                item['quantity'] * item['price']
                for item in cart.values()
            )
        })


class AnalyticsViewSet(viewsets.ViewSet):
    """
    ViewSet для аналитики и трекинга событий.

    Предоставляет:
        - track: POST /api/analytics/track/ - трекинг событий

    Permissions: Доступно для всех (CSRF exempt для внешних интеграций)
    """
    permission_classes = [AllowAny]

    @action(detail=False, methods=['post'], url_path='track')
    def track(self, request):
        """
        Трекинг событий для аналитики.

        Request Body:
            - event_type: Тип события (view, click, add_to_cart, purchase, etc.)
            - product_id: ID товара (optional)
            - category_id: ID категории (optional)
            - metadata: Дополнительные данные JSON (optional)

        Returns:
            - 200: Событие записано
            - 400: Ошибка валидации

        Example:
            POST /api/analytics/track/
            {
                "event_type": "add_to_cart",
                "product_id": 123,
                "metadata": {"source": "homepage"}
            }
        """
        serializer = TrackEventSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )

        validated_data = serializer.validated_data
        event_type = validated_data['event_type']
        product_id = validated_data.get('product_id')
        category_id = validated_data.get('category_id')
        metadata = validated_data.get('metadata', {})

        # TODO: Сохранить событие в БД или отправить в аналитику
        # Например: Google Analytics, Mixpanel, Amplitude, etc.

        import logging
        logger = logging.getLogger('storefront.analytics')
        logger.info(
            f"Event tracked: {event_type}, "
            f"Product: {product_id}, "
            f"Category: {category_id}, "
            f"Metadata: {metadata}"
        )

        return Response({
            'success': True,
            'message': 'Подію відстежено',
            'event_type': event_type
        })


@extend_schema(responses=OpenApiTypes.OBJECT)
class CommunicationViewSet(viewsets.ViewSet):
    """
    ViewSet для коммуникации с клиентами.

    Предоставляет:
        - newsletter: POST /api/communication/newsletter/ - подписка на рассылку
        - contact: POST /api/communication/contact/ - форма обратной связи

    Permissions: Доступно для всех
    """
    permission_classes = [AllowAny]

    @extend_schema(request=NewsletterSubscribeSerializer)
    @action(detail=False, methods=['post'], url_path='newsletter')
    def newsletter(self, request):
        """
        Подписка на email рассылку.

        Request Body:
            - email: Email адрес подписчика

        Returns:
            - 200: Подписка успешна
            - 400: Ошибка валидации

        Example:
            POST /api/communication/newsletter/
            {
                "email": "user@example.com"
            }
        """
        serializer = NewsletterSubscribeSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )

        email = serializer.validated_data['email']

        # TODO: Сохранить email в БД или отправить в сервис рассылок
        # Например: MailChimp, SendGrid, Mailgun, etc.

        import logging
        logger = logging.getLogger('storefront.newsletter')
        logger.info(f"Newsletter subscription: {email}")

        return Response({
            'success': True,
            'message': 'Дякуємо за підписку! Ви будете отримувати наші новини.'
        })

    @extend_schema(request=ContactFormSerializer)
    @action(detail=False, methods=['post'], url_path='contact')
    def contact(self, request):
        """
        Форма обратной связи.

        Request Body:
            - name: Имя отправителя
            - email: Email отправителя
            - phone: Телефон (optional)
            - message: Текст сообщения

        Returns:
            - 200: Сообщение отправлено
            - 400: Ошибка валидации

        Example:
            POST /api/communication/contact/
            {
                "name": "Іван Петренко",
                "email": "ivan@example.com",
                "phone": "+380501234567",
                "message": "Питання про доставку..."
            }
        """
        serializer = ContactFormSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )

        validated_data = serializer.validated_data
        name = validated_data['name']
        email = validated_data['email']
        phone = validated_data.get('phone', '')
        message = validated_data['message']

        # TODO: Отправить email администратору или сохранить в БД
        # Например: Django send_mail, Celery task, etc.

        import logging
        logger = logging.getLogger('storefront.contact')
        logger.info(
            f"Contact form submission: {name} ({email}), "
            f"Phone: {phone}, Message: {message[:50]}..."
        )

        return Response({
            'success': True,
            'message': 'Ваше повідомлення надіслано! Ми зв\'яжемося з вами найближчим часом.'
        })
