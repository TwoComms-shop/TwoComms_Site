"""
Storefront views package.

Рефакторинг views.py на модульную структуру.

Этот файл обеспечивает обратную совместимость, импортируя все views
из соответствующих модулей и из старого views.py.

Структура:
- utils.py - Утилиты и helper функции
- auth.py - Аутентификация (login, register, logout)
- catalog.py - Каталог товаров (в разработке)
- product.py - Детали товара (в разработке)
- cart.py - Корзина (в разработке)
- checkout.py - Оформление заказа (в разработке)
- profile.py - Профиль пользователя (в разработке)
- admin.py - Админ панель (в разработке)
- api.py - AJAX/API endpoints (в разработке)
- static_pages.py - Статические страницы (в разработке)
"""

import importlib.machinery
import importlib.util
from pathlib import Path

# ==================== НОВЫЕ МОДУЛИ ====================

# Утилиты
from .utils import (
    cache_page_for_anon,
    unique_slugify,
    get_cart_from_session,
    save_cart_to_session,
    calculate_cart_total,
    get_favorites_from_session,
    save_favorites_to_session,
    HOME_PRODUCTS_PER_PAGE,
)

# Аутентификация
from .auth import (
    # Forms
    LoginForm,
    RegisterForm,
    ProfileSetupForm,
    # Views
    login_view,
    register_view,
    logout_view,
)

# Каталог
from .catalog import (
    home,
    load_more_products,
    catalog,
    category_color_landing,
    thematic_landing,
    search,
)

# Товары
from .product import (
    product_detail,
    get_product_images,
    get_product_variants,
    quick_view,
)

# Корзина
from .cart import (
    view_cart,
    add_to_cart,
    update_cart,
    remove_from_cart,
    clear_cart,
    contact_manager,
    get_cart_count,
    apply_promo_code,
    remove_promo_code,
    cart_summary,
    cart_mini,
    cart_items_api,
    nova_poshta_city_search,
    nova_poshta_warehouse_search,
)

# Статические страницы
from .static_pages import (
    csp_report,
    robots_txt,
    llms_txt,
    llms_full_txt,
    custom_sitemap,
    static_sitemap,
    sitemap_section_static,
    sitemap_section_products,
    sitemap_section_product_variants,
    sitemap_section_categories,
    sitemap_section_color_categories,
    sitemap_section_thematic,
    sitemap_section_blog,
    sitemap_images,
    buyme_feed_xml,
    google_merchant_feed,
    kasta_feed_xml,
    rozetka_feed_xml,
    uaprom_products_feed,
    prom_feed_xml,
    static_verification_file,
    indexnow_key_file,
    about,
    contacts,
    custom_print,
    custom_print_add_to_cart,
    custom_print_lead,
    custom_print_remove,
    custom_print_safe_exit,
    custom_print_submit_review,
    custom_print_moderation_action,
    delivery,
    help_center,
    faq,
    size_guide,
    care_guide,
    order_tracking,
    site_map_page,
    news,
    returns,
    privacy_policy,
    terms_of_service,
    test_analytics_events,
)

from .blog import (
    blog_index,
    blog_category,
    blog_post,
    legacy_blog_redirect,
    admin_blog_category_create,
    admin_blog_category_update,
    admin_blog_post_create,
    admin_blog_post_update,
)

from .platform import (
    service_worker_script,
    web_manifest,
    favicon_ico,
)

# Профиль
from .profile import (
    profile,
    edit_profile,
    profile_setup,
    order_history,
    order_detail,
    favorites,
    favorites_list,
    toggle_favorite,
    add_to_favorites,
    remove_from_favorites,
    check_favorite_status,
    favorites_count,
    points_history,
    settings,
    # User Points & Rewards
    user_points,
    my_promocodes,
    buy_with_points,
    purchase_with_points,
)

# Промокоды
from .promo import (
    # Helper functions
    get_promo_admin_context,
    # Forms
    PromoCodeForm,
    PromoCodeGroupForm,
    # Admin views
    admin_promocodes,
    admin_promocode_create,
    admin_promocode_edit,
    admin_promocode_toggle,
    admin_promocode_delete,
    # Groups
    admin_promo_groups,
    admin_promo_group_create,
    admin_promo_group_edit,
    admin_promo_group_delete,
    # Statistics
    admin_promo_stats,
    # AJAX endpoints
    admin_promocode_get_form,
    admin_promocode_edit_ajax,
    admin_promo_group_get_form,
    admin_promo_group_edit_ajax,
    admin_promocode_change_group,
    admin_promo_export,
)

# API endpoints
from .api import (
    get_product_json,
    get_categories_json,
    track_event,
    search_suggestions,
    product_availability,
    get_related_products,
    newsletter_subscribe,
    contact_form,
)

# Surveys
from .survey import (
    survey_start_or_resume,
    survey_current_question,
    survey_submit_answer,
    survey_back_one_step,
    survey_close,
    survey_complete,
    survey_dismiss,
)

# Оформление заказа
from .checkout import (
    create_order,
    order_success,
    order_success_preview,
    update_payment_method,
    confirm_payment,
)


# Monobank оплата
from .monobank import (
    monobank_webhook,
    monobank_return,
    monobank_create_invoice,
)

# Админка
from .admin import (
    admin_panel,
    admin_dashboard,
    manage_products,
    add_product,
    admin_reorder_products,
    admin_update_product_status,
    admin_custom_print_lead_status,
    admin_custom_print_lead_moderation,
    admin_toggle_manager,
    add_category,
    add_print,
    manage_print_proposals,
    manage_promo_codes,
    generate_seo_content,
    generate_alt_texts,
    manage_orders,
    sales_statistics,
    inventory_management,
)

# Legacy Stubs (для обратной совместимости с urls.py)
from .legacy_stubs import (
    admin_offline_stores, admin_offline_store_create, admin_offline_store_edit,
    admin_offline_store_toggle, admin_offline_store_delete,
    admin_store_management, admin_store_add_product_to_order, admin_store_get_order_items,
    admin_store_get_product_colors, admin_store_remove_product_from_order,
    admin_store_add_products_to_store, admin_store_generate_invoice,
    admin_store_update_product, admin_store_mark_product_sold, admin_store_remove_product,
    admin_print_proposal_update_status, admin_print_proposal_award_points,
    admin_print_proposal_award_promocode,
    debug_media, debug_media_page, debug_product_images, dev_grant_admin,
    delivery_view, cooperation,
    pricelist_redirect, pricelist_page, test_pricelist, wholesale_page,
    wholesale_order_form, generate_wholesale_invoice, download_invoice_file,
    delete_wholesale_invoice, check_invoice_approval_status, check_payment_status,
    debug_invoices, create_wholesale_payment, wholesale_payment_webhook, get_user_invoices,
    admin_update_invoice_status, toggle_invoice_approval, toggle_invoice_payment_status,
    reset_all_invoices_status,
    admin_update_dropship_status, admin_get_dropship_order, admin_update_dropship_order,
    admin_delete_dropship_order,
    monobank_create_checkout
)

# ==================== LEGACY LOADER ====================
# Подгружаем критичные старые вьюхи из монолитного views.py при обращении к legacy-маршрутам.
_LEGACY_MODULE_LOADED = False
_LEGACY_VIEW_NAMES = (
    'admin_update_user',
    'admin_order_update',
    'admin_update_payment_status',
    'admin_approve_payment',
    'admin_order_delete',
    'admin_category_new',
    'admin_category_edit',
    'admin_category_delete',
    'admin_product_new',
    'admin_product_edit',
    'admin_product_edit_simple',
    'admin_product_edit_unified',
    'admin_product_delete',
    'admin_product_colors',
    'admin_product_color_delete',
    'admin_product_image_delete',
    # Wholesale & static
    'pricelist_redirect',
    'pricelist_page',
    'test_pricelist',
    'wholesale_page',
    'wholesale_order_form',
    'generate_wholesale_invoice',
    'download_invoice_file',
    'delete_wholesale_invoice',
    'check_invoice_approval_status',
    'check_payment_status',
    'debug_invoices',
    'create_wholesale_payment',
    'wholesale_payment_webhook',
    'get_user_invoices',
    'wholesale_prices_xlsx',
    'api_colors',
)


def _load_legacy_views(force: bool = False):
    """
    Ленивая загрузка функций из старого views.py (views.py.backup).
    Нужна для обратной совместимости маршрутов /admin-panel/product/new/ и др.
    """
    global _LEGACY_MODULE_LOADED
    if _LEGACY_MODULE_LOADED and not force:
        return

    legacy_path = Path(__file__).resolve().parent.parent / 'views.py.backup'
    if not legacy_path.exists():
        return

    loader = importlib.machinery.SourceFileLoader('storefront.legacy_views_backup', str(legacy_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if not spec or not spec.loader:
        return

    legacy_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy_module)

    for name in _LEGACY_VIEW_NAMES:
        if hasattr(legacy_module, name):
            globals()[name] = getattr(legacy_module, name)

    _LEGACY_MODULE_LOADED = True


# ==================== АЛИАСЫ ДЛЯ ОБРАТНОЙ СОВМЕСТИМОСТИ ====================
# Эти алиасы обеспечивают совместимость с старым views.py и storefront/urls.py

# Используем новую view_cart, она теперь обрабатывает POST (update_profile / guest / order_create)
cart = view_cart

# Cart aliases
cart_remove = remove_from_cart  # для urls.py: views.cart_remove
clean_cart = clear_cart  # для urls.py: views.clean_cart

# Profile aliases (если нужны)
profile_setup_db = profile_setup  # для urls.py: views.profile_setup_db

# Auth aliases (если нужны)
register_view_new = register_view  # для urls.py: views.register_view_new

# Checkout aliases
order_create = create_order

# Перехват данных оформления для брошенных корзин
from .checkout_capture import capture_checkout

# Profile aliases
my_orders = order_history


# ==================== ЭКСПОРТЫ ====================

__all__ = [
    # Utils
    'cache_page_for_anon', 'unique_slugify', 'get_cart_from_session',
    'save_cart_to_session', 'calculate_cart_total', 'get_favorites_from_session',
    'save_favorites_to_session', 'HOME_PRODUCTS_PER_PAGE',

    # Auth
    'LoginForm', 'RegisterForm', 'ProfileSetupForm',
    'login_view', 'register_view', 'logout_view',

    # Catalog
    'home', 'load_more_products', 'catalog', 'category_color_landing', 'thematic_landing', 'search',

    # Product
    'product_detail', 'get_product_images', 'get_product_variants', 'quick_view',

    # Cart
    'view_cart', 'add_to_cart', 'update_cart', 'remove_from_cart', 'clear_cart',
    'contact_manager', 'get_cart_count', 'apply_promo_code', 'remove_promo_code',
    'cart_summary', 'cart_mini', 'cart_items_api',
    'nova_poshta_city_search', 'nova_poshta_warehouse_search',

    # Static Pages
    'csp_report',
    'robots_txt', 'llms_txt', 'llms_full_txt', 'custom_sitemap', 'static_sitemap', 'sitemap_section_blog', 'google_merchant_feed', 'rozetka_feed_xml', 'kasta_feed_xml',
    'buyme_feed_xml',
    'static_verification_file', 'indexnow_key_file', 'about', 'contacts', 'custom_print', 'custom_print_lead',
    'custom_print_safe_exit', 'custom_print_add_to_cart', 'custom_print_remove',
    'custom_print_submit_review', 'custom_print_moderation_action',
    'delivery', 'help_center', 'faq', 'size_guide', 'care_guide',
    'order_tracking', 'site_map_page', 'news', 'returns',
    'privacy_policy', 'terms_of_service', 'test_analytics_events',
    'prom_feed_xml', 'uaprom_products_feed',

    # Blog
    'blog_index', 'blog_category', 'blog_post', 'legacy_blog_redirect',
    'admin_blog_category_create', 'admin_blog_category_update',
    'admin_blog_post_create', 'admin_blog_post_update',

    # Surveys
    'survey_start_or_resume', 'survey_current_question', 'survey_submit_answer',
    'survey_back_one_step', 'survey_close', 'survey_complete', 'survey_dismiss',

    # Profile
    'profile', 'edit_profile', 'profile_setup', 'order_history', 'order_detail',
    'favorites', 'favorites_list', 'toggle_favorite', 'add_to_favorites',
    'remove_from_favorites', 'check_favorite_status', 'favorites_count',
    'points_history', 'settings',
    'user_points', 'my_promocodes', 'buy_with_points', 'purchase_with_points',

    # Promo Codes
    'PromoCodeForm', 'PromoCodeGroupForm', 'get_promo_admin_context',
    'admin_promocodes', 'admin_promocode_create', 'admin_promocode_edit',
    'admin_promocode_toggle', 'admin_promocode_delete',
    'admin_promo_groups', 'admin_promo_group_create', 'admin_promo_group_edit',
    'admin_promo_group_delete', 'admin_promo_stats',
    'admin_promocode_get_form', 'admin_promocode_edit_ajax',
    'admin_promo_group_get_form', 'admin_promo_group_edit_ajax',
    'admin_promocode_change_group', 'admin_promo_export',

    # API
    'get_product_json', 'get_categories_json', 'track_event', 'search_suggestions',
    'product_availability', 'get_related_products', 'newsletter_subscribe', 'contact_form',

    # Admin
    'admin_panel', 'admin_dashboard', 'manage_products', 'add_product', 'add_category', 'add_print',
    'manage_print_proposals', 'manage_promo_codes', 'generate_seo_content',
    'generate_alt_texts', 'manage_orders', 'sales_statistics', 'inventory_management',
    'admin_reorder_products', 'admin_update_product_status',
    'admin_custom_print_lead_status',
    'admin_custom_print_lead_moderation',
    'admin_toggle_manager',

    # Aliases (для обратной совместимости)
    'cart', 'cart_remove', 'clean_cart', 'profile_setup_db', 'register_view_new',
    'order_create', 'my_orders', 'order_success', 'order_success_preview',
    'update_payment_method', 'confirm_payment', 'monobank_create_invoice',

    # Legacy Stubs
    'admin_offline_stores', 'admin_offline_store_create', 'admin_offline_store_edit',
    'admin_offline_store_toggle', 'admin_offline_store_delete',
    'admin_store_management', 'admin_store_add_product_to_order', 'admin_store_get_order_items',
    'admin_store_get_product_colors', 'admin_store_remove_product_from_order',
    'admin_store_add_products_to_store', 'admin_store_generate_invoice',
    'admin_store_update_product', 'admin_store_mark_product_sold', 'admin_store_remove_product',
    'admin_print_proposal_update_status', 'admin_print_proposal_award_points',
    'admin_print_proposal_award_promocode',
    'api_colors', 'debug_media', 'debug_media_page', 'debug_product_images', 'dev_grant_admin',
    'delivery_view', 'cooperation',
    'pricelist_redirect', 'pricelist_page', 'test_pricelist', 'wholesale_page',
    'wholesale_order_form', 'generate_wholesale_invoice', 'download_invoice_file',
    'delete_wholesale_invoice', 'check_invoice_approval_status', 'check_payment_status',
    'debug_invoices', 'create_wholesale_payment', 'wholesale_payment_webhook', 'get_user_invoices',
    'admin_update_invoice_status', 'toggle_invoice_approval', 'toggle_invoice_payment_status',
    'reset_all_invoices_status',
    'admin_update_dropship_status', 'admin_get_dropship_order', 'admin_update_dropship_order',
    'admin_delete_dropship_order',
    'monobank_webhook', 'monobank_return', 'monobank_create_checkout'
]
