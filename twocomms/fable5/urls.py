from django.urls import path

from . import views

urlpatterns = [
    # Єдиний редактор: додавання і редагування — ОДИН шаблон
    path("admin-panel/fable5/product/new/", views.editor, name="fable5_product_new"),
    path("admin-panel/fable5/product/<int:product_id>/", views.editor, name="fable5_product_edit"),

    # JSON API (AJAX, без перезавантаження сторінки)
    path("admin-panel/fable5/api/product/save/", views.api_product_save, name="fable5_api_product_save"),
    path("admin-panel/fable5/api/images/upload/", views.api_images_upload, name="fable5_api_images_upload"),
    path("admin-panel/fable5/api/images/update/", views.api_image_update, name="fable5_api_image_update"),
    path("admin-panel/fable5/api/images/reorder/", views.api_images_reorder, name="fable5_api_images_reorder"),
    path("admin-panel/fable5/api/images/set-cover/", views.api_set_cover, name="fable5_api_set_cover"),
    path("admin-panel/fable5/api/variant/save/", views.api_variant_save, name="fable5_api_variant_save"),
    path("admin-panel/fable5/api/variant/delete/", views.api_variant_delete, name="fable5_api_variant_delete"),
    path("admin-panel/fable5/api/variant/reorder/", views.api_variants_reorder, name="fable5_api_variants_reorder"),
    path("admin-panel/fable5/api/colors/", views.api_colors, name="fable5_api_colors"),
    path("admin-panel/fable5/api/slug/", views.api_slug_preview, name="fable5_api_slug"),
    path("admin-panel/fable5/api/stock/", views.api_stock, name="fable5_api_stock"),
    path("admin-panel/fable5/api/feeds/", views.api_feeds, name="fable5_api_feeds"),
    path("admin-panel/fable5/api/feeds/create/", views.api_feed_create, name="fable5_api_feed_create"),
    path("admin-panel/fable5/api/feeds/rule/", views.api_feed_rule_save, name="fable5_api_feed_rule_save"),
    path("admin-panel/fable5/api/feeds/image/upload/", views.api_feed_only_image_upload, name="fable5_api_feed_image_upload"),
    path("admin-panel/fable5/api/feeds/image/delete/", views.api_feed_only_image_delete, name="fable5_api_feed_image_delete"),
]
