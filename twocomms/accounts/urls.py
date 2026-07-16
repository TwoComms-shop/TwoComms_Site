"""
URLs для accounts приложения
"""
from django.urls import path
from . import ajax_auth_views, telegram_verify_views, telegram_views, views

urlpatterns = [
    path('ubd-document/<int:profile_id>/', views.ubd_document, name='ubd_document'),
    # Telegram (legacy webhook + старий username-based підтвердження)
    path('telegram/webhook/', telegram_views.telegram_webhook, name='telegram_webhook'),
    path('telegram/link/', telegram_views.link_telegram_account, name='link_telegram_account'),
    path('telegram/status/', telegram_views.check_telegram_status, name='check_telegram_status'),
    path('telegram/unlink/', telegram_views.unlink_telegram, name='unlink_telegram'),
    path('telegram/get-id/', telegram_views.get_telegram_id, name='get_telegram_id'),

    # Universal Telegram verification (через бот, поділитися номером)
    path(
        'telegram-verify/start/',
        telegram_verify_views.telegram_verify_start,
        name='telegram_verify_start',
    ),
    path(
        'telegram-verify/status/',
        telegram_verify_views.telegram_verify_status,
        name='telegram_verify_status',
    ),
    path(
        'telegram-verify/cancel/',
        telegram_verify_views.telegram_verify_cancel,
        name='telegram_verify_cancel',
    ),
    path(
        'telegram-login/complete/',
        telegram_verify_views.telegram_login_complete,
        name='telegram_login_complete',
    ),

    # AJAX Auth для дропшипа
    path('ajax/login/', ajax_auth_views.ajax_login, name='ajax_login'),
    path('ajax/register/', ajax_auth_views.ajax_register, name='ajax_register'),
]
