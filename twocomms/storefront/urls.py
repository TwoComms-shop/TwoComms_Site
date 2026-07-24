from django.conf import settings
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from django.views.generic.base import RedirectView
from django.http import Http404, HttpResponsePermanentRedirect
from . import views
from .view_loader import load_view_attr
# Import auth views from the modular auth.py module
from .views import auth as auth_views


def _legacy_view(name):
    """Lazy loader for legacy views living in the monolithic views.py."""

    def _wrapped(request, *args, **kwargs):
        from . import views as storefront_views

        if hasattr(storefront_views, '_load_legacy_views'):
            try:
                storefront_views._load_legacy_views(force=True)
            except Exception:
                # Even if legacy load fails, continue to 404 below
                pass

        handler = getattr(storefront_views, name, None)
        if handler is None:
            raise Http404(f"Legacy view '{name}' not found")
        return handler(request, *args, **kwargs)

    _wrapped.__name__ = name
    return _wrapped


def _module_view(module_path, attr_name):
    """Resolve modular views lazily so stale worker imports do not break URLConf."""

    def _wrapped(request, *args, **kwargs):
        try:
            handler = load_view_attr(module_path, attr_name)
        except AttributeError as exc:
            raise Http404(str(exc))
        return handler(request, *args, **kwargs)

    _wrapped.__name__ = attr_name
    return _wrapped


def _legacy_pagination_redirect(request, page=None):
    """SEO 2026-05-16 — 301 from legacy /page/N/ → /?page=N (homepage paginator).

    Search Console reported a cluster of /page/2/, /page/3/, … entries flagged
    "Crawled, currently not indexed" (the URL pattern was reachable on an older
    deploy and external links still point at it). Returning a permanent
    redirect to the canonical paginator URL preserves any inbound link equity
    and lets Google retire the dead pattern from the index.
    """
    try:
        page_number = int(page or 1)
    except (TypeError, ValueError):
        page_number = 1
    if page_number <= 1:
        target = "/"
    else:
        target = f"/?page={page_number}"
    return HttpResponsePermanentRedirect(target)


def admin_panel_view(request, *args, **kwargs):
    from .views.admin import admin_panel as _admin_panel

    return _admin_panel(request, *args, **kwargs)


urlpatterns = [
    # Legacy support URLs kept as permanent aliases to the canonical localized pages.
    path('help-center/', RedirectView.as_view(pattern_name='help_center', permanent=True), name='legacy_help_center'),
    path('kontakty/', RedirectView.as_view(pattern_name='contacts', permanent=True), name='legacy_contacts'),
    path('', views.home, name='home'),
    # SEO 2026-05-16 — legacy paginator URL pattern (existed on a previous
    # deploy, still receives external links). 301 to /?page=N consolidates
    # link equity instead of leaking it into a 404 cluster that Search
    # Console flags as "Crawled, currently not indexed".
    path('page/<int:page>/', _legacy_pagination_redirect, name='home_pagination_legacy'),
    path('load-more-products/', views.load_more_products, name='load_more_products'),
    path('catalog/', views.catalog, name='catalog'),
    # SEO 2026-05-16 — same legacy paginator pattern fix as on the homepage.
    path(
        'catalog/page/<int:page>/',
        lambda request, page: HttpResponsePermanentRedirect(
            f"/catalog/?page={page}" if int(page) > 1 else "/catalog/"
        ),
        name='catalog_pagination_legacy',
    ),
    # SEO molecular-upgrade US-5 (2026-05-16) — thematic landings.
    # MUST be defined BEFORE ``catalog/<slug:cat_slug>/`` because Django
    # URL routing is order-sensitive and the slug pattern would
    # otherwise capture ``theme`` as ``cat_slug`` and ``military`` as
    # ``color_slug``, returning 404 from the colour-landing view.
    # See ``views.catalog.thematic_landing`` for the keyword-match
    # algorithm and editorial copy table.
    path(
        'catalog/theme/<slug:theme_slug>/',
        views.thematic_landing,
        name='catalog_theme_landing',
    ),
    path('catalog/<slug:cat_slug>/', views.catalog, name='catalog_by_cat'),
    path(
        'catalog/<slug:cat_slug>/<slug:color_slug>/',
        views.category_color_landing,
        name='catalog_by_cat_color',
    ),
    path(
        'product/restock/subscribe/',
        _module_view('storefront.views.restock', 'restock_subscribe'),
        name='restock_subscribe',
    ),
    path('product/<slug:slug>/', views.product_detail, name='product'),
    path('product/<int:product_id>/images/', views.get_product_images, name='get_product_images'),
    path('product/<int:product_id>/variants/', views.get_product_variants, name='get_product_variants'),
    path('product/<int:product_id>/quick-view/', views.quick_view, name='quick_view'),
    # Phase 7.2 — path-style variant URLs. Up to three ``slug`` segments
    # capture any combination of size code / colour slug / fit code.
    # The view parses them content-addressably (not positionally), so
    # ``/product/x/m/`` and ``/product/x/black/`` both resolve cleanly.
    # All patterns share ``name='product'`` so ``reverse('product',
    # kwargs=...)`` picks the right arity automatically based on
    # which of ``v1/v2/v3`` the caller supplied.
    path('product/<slug:slug>/<slug:v1>/', views.product_detail, name='product'),
    path('product/<slug:slug>/<slug:v1>/<slug:v2>/', views.product_detail, name='product'),
    path('product/<slug:slug>/<slug:v1>/<slug:v2>/<slug:v3>/', views.product_detail, name='product'),
    path('add-product/', views.add_product, name='add_product'),
    path('admin-panel/product/add/', views.add_product, name='admin_add_product'),
    path('add-category/', views.add_category, name='add_category'),
    path('cart/', views.cart, name='cart'),
    path('cart/add/', views.add_to_cart, name='cart_add'),
    path('cart/update/', views.update_cart, name='update_cart'),
    path('cart/remove/', views.cart_remove, name='cart_remove'),
    path('cart/count/', views.get_cart_count, name='get_cart_count'),

    path('cart/summary/', views.cart_summary, name='cart_summary'),
    path('cart/mini/', views.cart_mini, name='cart_mini'),
    path('cart/items/', views.cart_items_api, name='cart_items_api'),
    path('cart/delivery/cities/', views.nova_poshta_city_search, name='cart_np_city_search'),
    path('cart/delivery/warehouses/', views.nova_poshta_warehouse_search, name='cart_np_warehouse_search'),
    path('push/subscribe/', _module_view('storefront.views.push', 'push_subscribe'), name='push_subscribe'),
    path('push/unsubscribe/', _module_view('storefront.views.push', 'push_unsubscribe'), name='push_unsubscribe'),
    path('push/events/', csrf_exempt(_module_view('storefront.views.push', 'push_delivery_event')), name='push_delivery_event'),
    # csrf_exempt здесь обязателен: _module_view — внешняя обёртка, CSRF
    # middleware смотрит её, а не внутренний @csrf_exempt на rum_beacon.
    path('api/rum/', csrf_exempt(_module_view('storefront.views.api', 'rum_beacon')), name='rum_beacon'),
    path('api/track-event/', csrf_exempt(_module_view('storefront.views.api', 'track_event')), name='track_event'),
    # W3-3: ленивая выдача csrftoken + analytics-кук (см. analytics_bootstrap)
    path('api/bootstrap/', _module_view('storefront.views.api', 'analytics_bootstrap'), name='analytics_bootstrap'),
    # W3-2: приёмник клиентских JS-ошибок (window.onerror → client_errors.log).
    # csrf_exempt на обёртке обязателен — как у rum_beacon выше.
    path('api/client-error/', csrf_exempt(_module_view('storefront.views.api', 'client_error')), name='client_error'),
    path('cart/clean/', views.clean_cart, name='clean_cart'),
    # path('checkout/', views.checkout, name='checkout'), # REMOVED: Dead code
    # auth - using modular auth views with proper password validation
    path('login/', auth_views.login_view, name='login'),
    path('logout/', auth_views.logout_view, name='logout'),
    path('register/', auth_views.register_view, name='register'),
    path('profile/setup/', views.profile_setup_db, name='profile_setup'),
    # admin panel
    path('admin-panel/', admin_panel_view, name='admin_panel'),
    path(
        'admin-panel/feeds/create/',
        _module_view('storefront.views.feeds_admin', 'admin_marketplace_feed_create'),
        name='admin_marketplace_feed_create',
    ),
    path(
        'admin-panel/feeds/<int:feed_id>/update/',
        _module_view('storefront.views.feeds_admin', 'admin_marketplace_feed_update'),
        name='admin_marketplace_feed_update',
    ),
    path(
        'admin-panel/feeds/<int:feed_id>/duplicate/',
        _module_view('storefront.views.feeds_admin', 'admin_marketplace_feed_duplicate'),
        name='admin_marketplace_feed_duplicate',
    ),
    path(
        'admin-panel/feeds/<int:feed_id>/delete/',
        _module_view('storefront.views.feeds_admin', 'admin_marketplace_feed_delete'),
        name='admin_marketplace_feed_delete',
    ),
    path(
        'admin-panel/feeds/<int:feed_id>/product-rule/',
        _module_view('storefront.views.feeds_admin', 'admin_marketplace_feed_product_rule'),
        name='admin_marketplace_feed_product_rule',
    ),
    path(
        'admin-panel/feeds/<int:feed_id>/validate/',
        _module_view('storefront.views.feeds_admin', 'admin_marketplace_feed_validate'),
        name='admin_marketplace_feed_validate',
    ),
    path(
        'admin-panel/feeds/<int:feed_id>/regenerate/',
        _module_view('storefront.views.feeds_admin', 'admin_marketplace_feed_regenerate'),
        name='admin_marketplace_feed_regenerate',
    ),
    path(
        'admin-panel/blog/category/create/',
        _module_view('storefront.views.blog', 'admin_blog_category_create'),
        name='admin_blog_category_create',
    ),
    path(
        'admin-panel/blog/category/<int:pk>/edit/',
        _module_view('storefront.views.blog', 'admin_blog_category_update'),
        name='admin_blog_category_update',
    ),
    path(
        'admin-panel/blog/category/<int:pk>/delete/',
        _module_view('storefront.views.blog', 'admin_blog_category_delete'),
        name='admin_blog_category_delete',
    ),
    path(
        'admin-panel/blog/post/create/',
        _module_view('storefront.views.blog', 'admin_blog_post_create'),
        name='admin_blog_post_create',
    ),
    path(
        'admin-panel/blog/post/<int:pk>/edit/',
        _module_view('storefront.views.blog', 'admin_blog_post_update'),
        name='admin_blog_post_update',
    ),
    path(
        'admin-panel/blog/post/<int:pk>/delete/',
        _module_view('storefront.views.blog', 'admin_blog_post_delete'),
        name='admin_blog_post_delete',
    ),
    path(
        'admin-panel/blog/post/<int:pk>/preview/',
        _module_view('storefront.views.blog', 'admin_blog_post_preview'),
        name='admin_blog_post_preview',
    ),
    path(
        'admin-panel/blog/post/preview/',
        _module_view('storefront.views.blog', 'admin_blog_post_preview_new'),
        name='admin_blog_post_preview_new',
    ),
    path(
        'admin-panel/blog/media/upload/',
        _module_view('storefront.views.blog', 'admin_blog_media_upload'),
        name='admin_blog_media_upload',
    ),
    # Phase 21 (PR-A1/A2) — review moderation + SEO overrides inside
    # the custom admin. ``staff_member_required`` is enforced inside
    # the view functions (they live in ``views.admin``).
    path(
        'admin-panel/reviews/<int:review_id>/action/',
        _module_view('storefront.views.admin', 'admin_review_action'),
        name='admin_review_action',
    ),
    path(
        'admin-panel/reviews/bulk/',
        _module_view('storefront.views.admin', 'admin_review_bulk'),
        name='admin_review_bulk',
    ),
    path(
        'admin-panel/seo/category/save/',
        _module_view('storefront.views.admin', 'admin_seo_category_save'),
        name='admin_seo_category_save',
    ),
    # Phase 21 (PR-A3) — colour-aware SEO override CRUD inline in the
    # custom admin (replaces Django-admin links in admin_seo_dashboard).
    path(
        'admin-panel/seo/color-override/<int:override_id>/save/',
        _module_view('storefront.views.admin', 'admin_color_seo_save'),
        name='admin_color_seo_save',
    ),
    path(
        'admin-panel/seo/color-override/create/',
        _module_view('storefront.views.admin', 'admin_color_seo_create'),
        name='admin_color_seo_create',
    ),
    path(
        'admin-panel/seo/color-override/<int:override_id>/delete/',
        _module_view('storefront.views.admin', 'admin_color_seo_delete'),
        name='admin_color_seo_delete',
    ),
    # Phase 21 (PR-A3) — Category showcase swatches (was Django admin only).
    path(
        'admin-panel/seo/category/swatches/save/',
        _module_view('storefront.views.admin', 'admin_category_swatches_save'),
        name='admin_category_swatches_save',
    ),
    path(
        'admin-panel/push-notifications/create/',
        _module_view('storefront.views.admin', 'admin_push_notifications_create'),
        name='admin_push_notifications_create',
    ),
    path(
        'admin-panel/push-notifications/<int:campaign_id>/send/',
        _module_view('storefront.views.admin', 'admin_push_notifications_send'),
        name='admin_push_notifications_send',
    ),
    path('admin-panel/update-user/', _legacy_view('admin_update_user'), name='admin_update_user'),
    path('admin-panel/order/update/', _legacy_view('admin_order_update'), name='admin_order_update'),
    path('admin-panel/order/update-status/', _legacy_view('admin_order_update'), name='admin_update_order_status'),
    path(
        'admin-panel/order/update-payment-status/',
        _module_view('storefront.views.admin', 'admin_update_payment_status'),
        name='admin_update_payment_status',
    ),
    path(
        'admin-panel/orders/payment-snapshots/',
        _module_view('storefront.views.admin', 'admin_order_payment_snapshots'),
        name='admin_order_payment_snapshots',
    ),
    # Ручне створення замовлень адміністратором
    path(
        'admin-panel/orders/manual/create/',
        _module_view('storefront.views.manual_orders', 'manual_order_create'),
        name='manual_order_create',
    ),
    path(
        'admin-panel/orders/<int:order_id>/edit/',
        _module_view('storefront.views.manual_orders', 'manual_order_edit'),
        name='manual_order_edit',
    ),
    path(
        'admin-panel/orders/<int:order_id>/edit-data/',
        _module_view('storefront.views.manual_orders', 'manual_order_edit_data'),
        name='manual_order_edit_data',
    ),
    path(
        'admin-panel/orders/<int:order_id>/nova-poshta/',
        _module_view('storefront.views.admin_order_actions', 'admin_order_nova_poshta_action'),
        name='admin_order_nova_poshta_action',
    ),
    path(
        'admin-panel/orders/<int:order_id>/warehouse-action/',
        _module_view('storefront.views.admin_order_actions', 'admin_order_warehouse_action'),
        name='admin_order_warehouse_action',
    ),
    # Analytics: managed exclusion list + session drilldown.
    path(
        'admin-panel/analytics/exclusions/',
        _module_view('storefront.views.admin_analytics_extras', 'admin_analytics_exclusions_list'),
        name='admin_analytics_exclusions_list',
    ),
    path(
        'admin-panel/analytics/exclusions/create/',
        _module_view('storefront.views.admin_analytics_extras', 'admin_analytics_exclusion_create'),
        name='admin_analytics_exclusion_create',
    ),
    path(
        'admin-panel/analytics/exclusions/<int:pk>/',
        _module_view('storefront.views.admin_analytics_extras', 'admin_analytics_exclusion_update'),
        name='admin_analytics_exclusion_update',
    ),
    path(
        'admin-panel/analytics/sessions/',
        _module_view('storefront.views.admin_analytics_extras', 'admin_analytics_sessions'),
        name='admin_analytics_sessions',
    ),
    path(
        'admin-panel/analytics/sessions/<int:session_id>/',
        _module_view('storefront.views.admin_analytics_extras', 'admin_analytics_session_detail'),
        name='admin_analytics_session_detail',
    ),
    path('admin-panel/order/approve-payment/', _legacy_view('admin_approve_payment'), name='admin_approve_payment'),
    path('admin-panel/order/<int:pk>/delete/', _legacy_view('admin_order_delete'), name='admin_order_delete'),
    path(
        'admin-panel/indexnow/submit/',
        _module_view('storefront.views.admin', 'admin_indexnow_submit'),
        name='admin_indexnow_submit',
    ),
    path(
        'admin-panel/google-indexing/submit/',
        _module_view('storefront.views.admin', 'admin_google_indexing_submit'),
        name='admin_google_indexing_submit',
    ),
    path(
        'admin-panel/google-indexing/history/',
        _module_view('storefront.views.admin', 'admin_google_indexing_history'),
        name='admin_google_indexing_history',
    ),
    path(
        'admin-panel/google-indexing/preview/',
        _module_view('storefront.views.admin', 'admin_google_indexing_preview'),
        name='admin_google_indexing_preview',
    ),
    path(
        'admin-panel/google-indexing/resolve/',
        _module_view('storefront.views.admin', 'admin_google_indexing_resolve'),
        name='admin_google_indexing_resolve',
    ),
    # Phase 11 — SEO admin: regenerate AI keywords/description for one object.
    path(
        'admin-panel/seo/ai/generate/',
        _module_view('storefront.views.admin', 'admin_seo_ai_generate'),
        name='admin_seo_ai_generate',
    ),
    path(
        'admin-panel/custom-print/<int:lead_id>/status/',
        _module_view('storefront.views.admin', 'admin_custom_print_lead_status'),
        name='admin_custom_print_lead_status',
    ),
    path(
        'admin-panel/custom-print/<int:lead_id>/moderation/',
        _module_view('storefront.views.admin', 'admin_custom_print_lead_moderation'),
        name='admin_custom_print_lead_moderation',
    ),
    # orders
    path('orders/create/', views.order_create, name='order_create'),
    path('checkout/capture/', views.capture_checkout, name='checkout_capture'),
    path('orders/success/<int:order_id>/', views.order_success, name='order_success'),
    path('orders/success-preview/', views.order_success_preview, name='order_success_preview'),  # Тестовый preview
    path(
        'orders/telegram-action/<int:order_id>/<str:status>/',
        _module_view('storefront.views.order_actions', 'telegram_order_status_action'),
        name='telegram_order_status_action',
    ),
    path(
        'orders/telegram-waybill/<int:order_id>/<str:action>/',
        _module_view('storefront.views.order_actions', 'telegram_order_np_waybill_action'),
        name='telegram_order_np_waybill_action',
    ),
    path(
        'orders/telegram-waybill/<int:order_id>/<str:action>/payment-snapshot/',
        _module_view('storefront.views.order_actions', 'telegram_order_payment_snapshot'),
        name='telegram_order_payment_snapshot',
    ),
    path('my/orders/', views.my_orders, name='my_orders'),
    path('orders/update-payment-method/', views.update_payment_method, name='update_payment_method'),
    path('orders/confirm-payment/', views.confirm_payment, name='confirm_payment'),

    path('user/points/', views.user_points, name='user_points'),
    path('my-promocodes/', views.my_promocodes, name='my_promocodes'),
    path('qr/', _module_view('storefront.views.qr', 'qr_thanks'), name='qr_thanks'),
    path('buy-with-points/', views.buy_with_points, name='buy_with_points'),
    path('purchase-with-points/', views.purchase_with_points, name='purchase_with_points'),
    # catalogs CRUD
    path('admin-panel/category/new/', _legacy_view('admin_category_new'), name='admin_category_new'),
    path('admin-panel/category/<int:pk>/edit/', _legacy_view('admin_category_edit'), name='admin_category_edit'),
    path('admin-panel/category/<int:pk>/delete/', _legacy_view('admin_category_delete'), name='admin_category_delete'),
    path('admin-panel/product/new/', _legacy_view('admin_product_new'), name='admin_product_new'),
    path('admin-panel/product/builder/', _module_view('storefront.views.admin', 'admin_product_builder'), name='admin_product_builder'),
    path('admin-panel/product/<int:product_id>/builder/', _module_view('storefront.views.admin', 'admin_product_builder'), name='admin_product_builder_edit'),
    path('admin-panel/product/<int:pk>/edit/', _legacy_view('admin_product_edit'), name='admin_product_edit'),
    path('admin-panel/product/<int:pk>/edit-simple/', _legacy_view('admin_product_edit_simple'), name='admin_product_edit_simple'),
    path('admin-panel/product/<int:pk>/edit-unified/', _legacy_view('admin_product_edit_unified'), name='admin_product_edit_unified'),
    path('admin-panel/product/<int:pk>/delete/', _legacy_view('admin_product_delete'), name='admin_product_delete'),
    path('admin-panel/product/<int:pk>/colors/', _legacy_view('admin_product_colors'), name='admin_product_colors'),
    path('admin-panel/product/<int:product_pk>/color/<int:color_pk>/delete/', _legacy_view('admin_product_color_delete'), name='admin_product_color_delete'),
    path('admin-panel/product/<int:product_pk>/image/<int:image_pk>/delete/', _legacy_view('admin_product_image_delete'), name='admin_product_image_delete'),
    path('admin-panel/products/reorder/', views.admin_reorder_products, name='admin_reorder_products'),
    path('admin-panel/products/update-status/', views.admin_update_product_status, name='admin_update_product_status'),
    # promocodes
    path('admin-panel/promocodes/', views.admin_promocodes, name='admin_promocodes'),
    path('admin-panel/promocode/create/', views.admin_promocode_create, name='admin_promocode_create'),
    path('admin-panel/promocode/<int:pk>/edit/', views.admin_promocode_edit, name='admin_promocode_edit'),
    path('admin-panel/promocode/<int:pk>/toggle/', views.admin_promocode_toggle, name='admin_promocode_toggle'),
    path('admin-panel/promocode/<int:pk>/delete/', views.admin_promocode_delete, name='admin_promocode_delete'),
    # promo groups
    path('admin-panel/promo-groups/', views.admin_promo_groups, name='admin_promo_groups'),
    path('admin-panel/promo-group/create/', views.admin_promo_group_create, name='admin_promo_group_create'),
    path('admin-panel/promo-group/<int:pk>/edit/', views.admin_promo_group_edit, name='admin_promo_group_edit'),
    path('admin-panel/promo-group/<int:pk>/delete/', views.admin_promo_group_delete, name='admin_promo_group_delete'),
    # promo statistics
    path('admin-panel/promo-stats/', views.admin_promo_stats, name='admin_promo_stats'),
    # promo export
    path('admin-panel/promo-export/', views.admin_promo_export, name='admin_promo_export'),
    # promo AJAX endpoints
    path('admin-panel/promocode/<int:pk>/get-form/', views.admin_promocode_get_form, name='admin_promocode_get_form'),
    path('admin-panel/promocode/<int:pk>/edit-ajax/', views.admin_promocode_edit_ajax, name='admin_promocode_edit_ajax'),
    path('admin-panel/promocode/<int:pk>/change-group/', views.admin_promocode_change_group, name='admin_promocode_change_group'),
    path('admin-panel/promo-group/<int:pk>/get-form/', views.admin_promo_group_get_form, name='admin_promo_group_get_form'),
    path('admin-panel/promo-group/<int:pk>/edit-ajax/', views.admin_promo_group_edit_ajax, name='admin_promo_group_edit_ajax'),
    path('admin-panel/user/<int:user_id>/toggle-manager/', views.admin_toggle_manager, name='admin_toggle_manager'),
    # offline stores
    path('admin-panel/offline-stores/', views.admin_offline_stores, name='admin_offline_stores'),
    path('admin-panel/offline-store/create/', views.admin_offline_store_create, name='admin_offline_store_create'),
    path('admin-panel/offline-store/<int:pk>/edit/', views.admin_offline_store_edit, name='admin_offline_store_edit'),
    path('admin-panel/offline-store/<int:pk>/toggle/', views.admin_offline_store_toggle, name='admin_offline_store_toggle'),
    path('admin-panel/offline-store/<int:pk>/delete/', views.admin_offline_store_delete, name='admin_offline_store_delete'),
    # store management
    path('admin-panel/offline-store/<int:store_id>/manage/', views.admin_store_management, name='admin_store_management'),
    path('admin-panel/offline-store/<int:store_id>/add-to-order/', views.admin_store_add_product_to_order, name='admin_store_add_product_to_order'),
    path('admin-panel/offline-store/<int:store_id>/order/<int:order_id>/items/', views.admin_store_get_order_items, name='admin_store_get_order_items'),
    path('admin-panel/offline-store/<int:store_id>/product/<int:product_id>/colors/', views.admin_store_get_product_colors, name='admin_store_get_product_colors'),
    path('admin-panel/offline-store/<int:store_id>/order/<int:order_id>/remove-item/<int:item_id>/', views.admin_store_remove_product_from_order, name='admin_store_remove_product_from_order'),
    path('admin-panel/offline-store/<int:store_id>/add-to-store/', views.admin_store_add_products_to_store, name='admin_store_add_products_to_store'),
    path('admin-panel/offline-store/<int:store_id>/generate-invoice/', views.admin_store_generate_invoice, name='admin_store_generate_invoice'),
    path('admin-panel/offline-store/<int:store_id>/product/<int:product_id>/update/', views.admin_store_update_product, name='admin_store_update_product'),
    path('admin-panel/offline-store/<int:store_id>/product/<int:product_id>/mark-sold/', views.admin_store_mark_product_sold, name='admin_store_mark_product_sold'),
    path('admin-panel/offline-store/<int:store_id>/product/<int:product_id>/remove/', views.admin_store_remove_product, name='admin_store_remove_product'),
    # print proposals
    path('admin-panel/print-proposal/update-status/', views.admin_print_proposal_update_status, name='admin_print_proposal_update_status'),
    path('admin-panel/print-proposal/award-points/', views.admin_print_proposal_award_points, name='admin_print_proposal_award_points'),
    path('admin-panel/print-proposal/award-promocode/', views.admin_print_proposal_award_promocode, name='admin_print_proposal_award_promocode'),
    # promocodes in cart
    path('cart/apply-promo/', views.apply_promo_code, name='apply_promo_code'),
    path('cart/remove-promo/', views.remove_promo_code, name='remove_promo_code'),
    # Contact manager
    path('cart/contact-manager/', views.contact_manager, name='contact_manager'),
    # Monobank acquiring
    path('cart/monobank/create-invoice/', views.monobank_create_invoice, name='monobank_create_invoice'),
    path('cart/monobank/quick/', _legacy_view('monobank_create_checkout'), name='monobank_quick_invoice'),
    path('cart/order/<int:order_id>/send-receipt/', _module_view('orders.receipt_views', 'send_order_receipt'), name='order_send_receipt'),
    path('payments/monobank/return/', views.monobank_return, name='monobank_return'),
    path('payments/monobank/webhook/', csrf_exempt(views.monobank_webhook), name='monobank_webhook'),
    # API endpoints
    path('api/colors/', _legacy_view('api_colors'), name='api_colors'),
    # static pages
    path('add-print/', views.add_print, name='add_print'),
    path('custom-print/', _module_view('storefront.views.static_pages', 'custom_print'), name='custom_print'),
    path('custom-print/lead/', _module_view('storefront.views.static_pages', 'custom_print_lead'), name='custom_print_lead'),
    path(
        'custom-print/safe-exit/',
        _module_view('storefront.views.static_pages', 'custom_print_safe_exit'),
        name='custom_print_safe_exit',
    ),
    path(
        'custom-print/add-to-cart/',
        _module_view('storefront.views.static_pages', 'custom_print_add_to_cart'),
        name='custom_print_add_to_cart',
    ),
    path(
        'custom-print/remove/',
        _module_view('storefront.views.static_pages', 'custom_print_remove'),
        name='custom_print_remove',
    ),
    path(
        'custom-print/submit-review/',
        _module_view('storefront.views.static_pages', 'custom_print_submit_review'),
        name='custom_print_submit_review',
    ),
    path(
        'custom-print/moderation/<int:lead_id>/<str:action>/',
        _module_view('storefront.views.static_pages', 'custom_print_moderation_action'),
        name='custom_print_moderation_action',
    ),
    # Telegram contact verification flow (через бота заказов)
    path(
        'custom-print/telegram-verify/start/',
        _module_view('storefront.views.telegram_verify', 'telegram_verify_start'),
        name='custom_print_telegram_verify_start',
    ),
    path(
        'custom-print/telegram-verify/status/',
        _module_view('storefront.views.telegram_verify', 'telegram_verify_status'),
        name='custom_print_telegram_verify_status',
    ),
    path(
        'custom-print/telegram-verify/cancel/',
        _module_view('storefront.views.telegram_verify', 'telegram_verify_cancel'),
        name='custom_print_telegram_verify_cancel',
    ),
    path('delivery/', _module_view('storefront.views.static_pages', 'delivery'), name='delivery'),
    path('cooperation/', views.cooperation, name='cooperation'),
    path('pro-brand/', views.about, name='about'),
    path('about/', RedirectView.as_view(url='/pro-brand/', permanent=True), name='about_legacy'),
    path('blog', _module_view('storefront.views.blog', 'legacy_blog_redirect'), name='blog_no_slash'),
    path('blog/', _module_view('storefront.views.blog', 'blog_index'), name='blog'),
    path(
        'blog/category/<slug:parent_slug>/<slug:slug>/',
        _module_view('storefront.views.blog', 'blog_category'),
        name='blog_category_nested',
    ),
    path('blog/category/<slug:slug>/', _module_view('storefront.views.blog', 'blog_category'), name='blog_category'),
    path(
        'blog/<slug:slug>/promo/<int:block_id>/claim/',
        _module_view('storefront.views.blog', 'blog_promo_claim'),
        name='blog_promo_claim',
    ),
    path('blog/<slug:slug>/', _module_view('storefront.views.blog', 'blog_post'), name='blog_post'),
    path('news', _module_view('storefront.views.blog', 'legacy_blog_redirect'), name='news_no_slash'),
    path('news/', _module_view('storefront.views.blog', 'legacy_blog_redirect'), name='news_legacy'),
    path('novyny', _module_view('storefront.views.blog', 'legacy_blog_redirect'), name='novyny_no_slash'),
    path('dopomoga/', _module_view('storefront.views.static_pages', 'help_center'), name='help_center'),
    path('faq/', _module_view('storefront.views.static_pages', 'faq'), name='faq'),
    path('rozmirna-sitka/', _module_view('storefront.views.static_pages', 'size_guide'), name='size_guide'),
    path('doglyad-za-odyagom/', _module_view('storefront.views.static_pages', 'care_guide'), name='care_guide'),
    path('vidstezhennya-zamovlennya/', _module_view('storefront.views.static_pages', 'order_tracking'), name='order_tracking'),
    path('mapa-saytu/', _module_view('storefront.views.static_pages', 'site_map_page'), name='site_map_page'),
    path('novyny/', _module_view('storefront.views.blog', 'legacy_blog_redirect'), name='news'),
    # SEO v1.0 Phase 8 (2026-05-12) — finding (B21). Liveness probe used
    # by external uptime monitoring (BetterStack/UptimeRobot/Pingdom).
    # Returns 200 with a minimal JSON payload, no DB hits.
    path('healthz/', _module_view('storefront.views.static_pages', 'healthz'), name='healthz'),
    path('povernennya-ta-obmin/', _module_view('storefront.views.static_pages', 'returns'), name='returns'),
    path('polityka-konfidentsiynosti/', _module_view('storefront.views.static_pages', 'privacy_policy'), name='privacy_policy'),
    path('umovy-vykorystannya/', _module_view('storefront.views.static_pages', 'terms_of_service'), name='terms_of_service'),
    # Google Merchant Center
    path('google-merchant-feed.xml', views.google_merchant_feed, name='google_merchant_feed'),
    # alternate no-cache path
    path('google-merchant-feed-v2.xml', views.google_merchant_feed, name='google_merchant_feed_v2'),
    # Rozetka marketplace feed
    path('rozetka-feed.xml', views.rozetka_feed_xml, name='rozetka_feed_xml'),
    path('rozetka.xml', views.rozetka_feed_xml, name='rozetka_feed_xml_short'),
    # Kasta marketplace feed
    path('kasta-feed.xml', views.kasta_feed_xml, name='kasta_feed_xml'),
    path('kasta.xml', views.kasta_feed_xml, name='kasta_feed_xml_short'),
    # BuyMe marketplace feed
    path('buyme-feed.xml', views.buyme_feed_xml, name='buyme_feed_xml'),
    path('buyme.xml', views.buyme_feed_xml, name='buyme_feed_xml_short'),
    # UAPROM-style product feed (legacy Bezet feed)
    path('products_feed.xml', views.uaprom_products_feed, name='uaprom_products_feed'),
    # New Dynamic Prom.ua feed
    path('prom-feed.xml', views.prom_feed_xml, name='prom_feed_xml'),
    path(
        'feeds/<slug:slug>.xml',
        _module_view('storefront.views.feeds_admin', 'custom_marketplace_feed'),
        name='custom_marketplace_feed',
    ),
    # Try to intercept media path (might be blocked by Nginx, but worth adding for user request)
    path('media/prom-feed.xml', views.prom_feed_xml, name='prom_feed_xml_legacy_path'),

    path('contacts/', views.contacts, name='contacts'),
    path('search/', views.search, name='search'),
    # favorites
    path('favorites/', views.favorites_list, name='favorites'),
    path('favorites/toggle/<int:product_id>/', views.toggle_favorite, name='toggle_favorite'),
    path('favorites/check/<int:product_id>/', views.check_favorite_status, name='check_favorite_status'),
    path('favorites/count/', views.favorites_count, name='favorites_count'),
    # survey
    path('survey/print-feedback/start/', views.survey_start_or_resume, name='survey_start_or_resume'),
    path('survey/print-feedback/current/', views.survey_current_question, name='survey_current_question'),
    path('survey/print-feedback/answer/', views.survey_submit_answer, name='survey_submit_answer'),
    path('survey/print-feedback/back/', views.survey_back_one_step, name='survey_back_one_step'),
    path('survey/print-feedback/close/', views.survey_close, name='survey_close'),
    path('survey/print-feedback/complete/', views.survey_complete, name='survey_complete'),
    path('survey/print-feedback/dismiss/', views.survey_dismiss, name='survey_dismiss'),
    # wholesale prices
    path('pricelist_opt.xlsx', _legacy_view('wholesale_prices_xlsx'), name='wholesale_prices_xlsx'),
    path('pricelist/', _module_view('storefront.views.legacy_stubs', 'pricelist_page'), name='pricelist_page'),
    path('wholesale/', _module_view('storefront.views.legacy_stubs', 'wholesale_page'), name='wholesale_page'),
    path('opt/', RedirectView.as_view(url='/wholesale/', permanent=True), name='wholesale_page_alt'),
    path('wholesale/order-form/', _module_view('storefront.views.legacy_stubs', 'wholesale_order_form'), name='wholesale_order_form'),
    path('wholesale/generate-invoice/', _legacy_view('generate_wholesale_invoice'), name='generate_wholesale_invoice'),
    path('wholesale/download-invoice/<int:invoice_id>/', _legacy_view('download_invoice_file'), name='download_invoice_file'),
    path('wholesale/delete-invoice/<int:invoice_id>/', _legacy_view('delete_wholesale_invoice'), name='delete_wholesale_invoice'),
    path('wholesale/check-approval/<int:invoice_id>/', _legacy_view('check_invoice_approval_status'), name='check_invoice_approval_status'),
    path('wholesale/check-payment-status/<int:invoice_id>/', _legacy_view('check_payment_status'), name='check_payment_status'),
    path('wholesale/create-payment/', _legacy_view('create_wholesale_payment'), name='create_wholesale_payment'),
    path('wholesale/payment-webhook/', _legacy_view('wholesale_payment_webhook'), name='wholesale_payment_webhook'),
    path('wholesale/invoices/', _legacy_view('get_user_invoices'), name='get_user_invoices'),
    # Collaboration admin (staff-only) - теперь в admin_panel?section=collaboration
    path('admin-panel/invoices/update-status/<int:invoice_id>/', views.admin_update_invoice_status, name='admin_update_invoice_status'),
    path('admin-panel/invoices/toggle-approval/<int:invoice_id>/', views.toggle_invoice_approval, name='toggle_invoice_approval'),
    path('admin-panel/invoices/toggle-payment/<int:invoice_id>/', views.toggle_invoice_payment_status, name='toggle_invoice_payment_status'),
    path('admin-panel/invoices/reset-all-status/', views.reset_all_invoices_status, name='reset_all_invoices_status'),
    # Dropship orders admin
    path('admin-panel/dropship/update-status/<int:order_id>/', views.admin_update_dropship_status, name='admin_update_dropship_status'),
    path('admin-panel/dropship/get-order/<int:order_id>/', views.admin_get_dropship_order, name='admin_get_dropship_order'),
    path('admin-panel/dropship/update-order/<int:order_id>/', views.admin_update_dropship_order, name='admin_update_dropship_order'),
    path('admin-panel/dropship/delete-order/<int:order_id>/', views.admin_delete_dropship_order, name='admin_delete_dropship_order'),
]


# F-010: these tools must not exist in the production URL resolver. Keeping
# their registration behind DEBUG preserves local diagnostics without exposing
# route names or login redirects on the public host.
if settings.DEBUG:
    urlpatterns += [
        path('debug/media/', views.debug_media, name='debug_media'),
        path('debug/media-page/', views.debug_media_page, name='debug_media_page'),
        path('debug/product-images/', views.debug_product_images, name='debug_product_images'),
        path('dev/grant-admin/', views.dev_grant_admin, name='dev_grant_admin'),
        path('test-analytics/', views.test_analytics_events, name='test_analytics'),
        path('test-pricelist/', _legacy_view('test_pricelist'), name='test_wholesale_prices'),
        path('wholesale/debug-invoices/', _legacy_view('debug_invoices'), name='debug_invoices'),
    ]
