"""URL-маршрути фінансового кабінету (fin.twocomms.shop).

Розділи за ТЗ 01 §3: /, /analytic, /ai, /calendar, /invoices, /rules, /users.
"""
from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    # --- Авторизація ---
    path('login/', auth_views.LoginView.as_view(
        template_name='finance/login.html',
        redirect_authenticated_user=True,
    ), name='finance_login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='finance_login'), name='finance_logout'),

    # --- Розділи ---
    path('health/', views.financial_health, name='finance_health'),
    path('grants/', views.grants, name='finance_grants'),
    # --- Новий reviewable ledger/API слой ---
    path('api/v2/health/', views.finance_v2_health_api, name='finance_v2_health_api'),
    path('api/v2/transactions/<int:txn_id>/classification/', views.classification_api, name='finance_v2_classification_api'),
    path('api/v2/reviews/', views.review_list_api, name='finance_v2_review_list_api'),
    path('api/v2/reviews/<int:review_id>/action/', views.review_action_api, name='finance_v2_review_action_api'),
    path('api/v2/transfers/suggestions/', views.transfer_suggestions_api, name='finance_v2_transfer_suggestions_api'),
    path('api/v2/funding/', views.funding_sources_api, name='finance_v2_funding_sources_api'),
    path('api/v2/funding/<int:source_id>/allocate/', views.funding_allocate_api, name='finance_v2_funding_allocate_api'),
    path('api/v2/obligation-groups/', views.obligation_groups_api, name='finance_v2_obligation_groups_api'),
    path('api/v2/obligation-groups/<int:group_id>/', views.obligation_group_api, name='finance_v2_obligation_group_api'),
    path('api/v2/obligation-groups/<int:group_id>/components/', views.obligation_component_api, name='finance_v2_obligation_component_api'),
    path('api/v2/obligation-components/<int:component_id>/payment-intent/', views.component_payment_intent_api, name='finance_v2_component_payment_intent_api'),
    path('api/v2/obligation-components/<int:component_id>/payment-context/', views.component_payment_context_api, name='finance_v2_component_payment_context_api'),
    path('api/v2/obligation-components/<int:component_id>/settle-existing/', views.component_settle_existing_api, name='finance_v2_component_settle_existing_api'),
    path('api/v2/payment-intents/<int:intent_id>/', views.payment_intent_api, name='finance_v2_payment_intent_api'),
    path('api/v2/payment-intents/<int:intent_id>/transition/', views.payment_intent_transition_api, name='finance_v2_payment_intent_transition_api'),
    path('api/v2/payment-intents/<int:intent_id>/match/', views.payment_intent_match_api, name='finance_v2_payment_intent_match_api'),
    path('api/v2/settlements/<int:settlement_id>/components/', views.settlement_components_api, name='finance_v2_settlement_components_api'),
    path('api/v2/refunds/link/', views.refund_link_api, name='finance_v2_refund_link_api'),
    # Магазини під реалізацію (consignment)
    path('consignment/', views.consignment_list, name='finance_consignment'),
    path('consignment/<int:reseller_id>/', views.consignment_detail, name='finance_consignment_detail'),
    path('api/consignment/resellers/create/', views.consignment_reseller_create_api, name='finance_consignment_reseller_create_api'),
    path('api/consignment/resellers/<int:reseller_id>/update/', views.consignment_reseller_update_api, name='finance_consignment_reseller_update_api'),
    path('api/consignment/resellers/<int:reseller_id>/delete/', views.consignment_reseller_delete_api, name='finance_consignment_reseller_delete_api'),
    path('api/consignment/resellers/<int:reseller_id>/shipments/create/', views.consignment_shipment_create_api, name='finance_consignment_shipment_create_api'),
    path('api/consignment/shipments/<int:shipment_id>/delete/', views.consignment_shipment_delete_api, name='finance_consignment_shipment_delete_api'),
    path('api/consignment/items/<int:item_id>/sale/', views.consignment_sale_create_api, name='finance_consignment_sale_create_api'),
    path('api/consignment/resellers/<int:reseller_id>/payable-txns/', views.consignment_payable_txns_api, name='finance_consignment_payable_txns_api'),
    path('api/consignment/resellers/<int:reseller_id>/payment/', views.consignment_payment_api, name='finance_consignment_payment_api'),
    path('api/consignment/resellers/<int:reseller_id>/stats/', views.consignment_stats_api, name='finance_consignment_stats_api'),
    path('api/consignment/resellers/<int:reseller_id>/get/', views.consignment_reseller_get_api, name='finance_consignment_reseller_get_api'),
    path('api/consignment/management/orders/', views.consignment_management_orders_api, name='finance_consignment_mgmt_orders_api'),
    path('api/consignment/management/tests/', views.consignment_management_tests_api, name='finance_consignment_mgmt_tests_api'),
    path('api/consignment/management/orders/<int:invoice_id>/items/', views.consignment_management_order_items_api, name='finance_consignment_mgmt_order_items_api'),
    path('api/consignment/management/tests/<int:shop_id>/items/', views.consignment_management_test_items_api, name='finance_consignment_mgmt_test_items_api'),
    path('analytic/', views.analytics, name='finance_analytics'),
    path('analytic/report/<str:kind>/', views.report, name='finance_report'),
    path('analytic/report/<str:kind>/export/', views.report_export, name='finance_report_export'),
    path('api/metrics/create/', views.metric_create_api, name='finance_metric_create_api'),
    path('api/debt/<int:txn_id>/settle/', views.debt_settle_api, name='finance_debt_settle_api'),
    path('ai/', views.ai_advisor_page, name='finance_ai'),
    path('api/ai/chat/', views.ai_chat_api, name='finance_ai_chat_api'),
    path('api/ai/check-payments/', views.ai_check_payments_api, name='finance_ai_check_payments_api'),
    path('api/ai/check-report/', views.ai_check_report_api, name='finance_ai_check_report_api'),
    path('calendar/', views.calendar, name='finance_calendar'),
    path('invoices/', views.invoices, name='finance_invoices'),
    path('invoices/new/', views.invoice_form, name='finance_invoice_new'),
    path('invoices/<int:invoice_id>/edit/', views.invoice_form, name='finance_invoice_edit'),
    path('invoices/<int:invoice_id>/print/', views.invoice_print, name='finance_invoice_print'),
    path('api/invoices/save/', views.invoice_save_api, name='finance_invoice_save_api'),
    path('api/invoices/<int:invoice_id>/save/', views.invoice_save_api, name='finance_invoice_update_api'),
    path('api/invoices/<int:invoice_id>/delete/', views.invoice_delete_api, name='finance_invoice_delete_api'),
    path('api/invoices/<int:invoice_id>/pay/', views.invoice_pay_api, name='finance_invoice_pay_api'),
    path('rules/', views.rules, name='finance_rules'),
    path('api/rules/save/', views.rule_save_api, name='finance_rule_save_api'),
    path('api/rules/<int:rule_id>/save/', views.rule_save_api, name='finance_rule_update_api'),
    path('api/rules/<int:rule_id>/toggle/', views.rule_toggle_api, name='finance_rule_toggle_api'),
    path('api/rules/<int:rule_id>/delete/', views.rule_delete_api, name='finance_rule_delete_api'),
    path('api/rules/<int:rule_id>/preview/', views.rule_preview_api, name='finance_rule_preview_api'),
    path('api/rules/<int:rule_id>/apply/', views.rule_apply_api, name='finance_rule_apply_api'),
    path('users/', views.users, name='finance_users'),
    path('accounts/', views.accounts, name='finance_accounts'),

    # --- Контрагенти ---
    path('counterparties/', views.counterparties, name='finance_counterparties'),
    path('counterparties/<int:counterparty_id>/', views.counterparty_detail_page, name='finance_counterparty_detail'),
    path('api/counterparties/create/', views.counterparty_create_api, name='finance_counterparty_create_api'),
    path('api/counterparties/<int:counterparty_id>/get/', views.counterparty_get_api, name='finance_counterparty_get_api'),
    path('api/counterparties/<int:counterparty_id>/update/', views.counterparty_update_api, name='finance_counterparty_update_api'),
    path('api/counterparties/<int:counterparty_id>/delete/', views.counterparty_delete_api, name='finance_counterparty_delete_api'),
    path('api/counterparties/<int:counterparty_id>/cards/', views.counterparty_cards_api, name='finance_counterparty_cards_api'),
    path('api/counterparties/<int:counterparty_id>/cards/save/', views.counterparty_card_save_api, name='finance_counterparty_card_save_api'),
    path('api/counterparties/<int:counterparty_id>/cards/<int:card_id>/delete/', views.counterparty_card_delete_api, name='finance_counterparty_card_delete_api'),

    # --- Планові платежі (зобов'язання) ---
    path('planned/', views.planned, name='finance_planned'),
    path('api/planned/obligations/', views.planned_obligations_api, name='finance_planned_obligations_api'),
    path('api/obligations/<int:txn_id>/settle-context/', views.obligation_settle_context_api, name='finance_obligation_settle_context_api'),
    path('api/obligations/<int:txn_id>/settle/', views.obligation_settle_api, name='finance_obligation_settle_api'),
    path('api/obligations/<int:txn_id>/skip/', views.obligation_skip_api, name='finance_obligation_skip_api'),
    path('api/obligations/<int:txn_id>/move-current/', views.obligation_move_current_api, name='finance_obligation_move_current_api'),
    path('api/payments/<int:txn_id>/reverse-candidates/', views.payment_reverse_candidates_api, name='finance_payment_reverse_candidates_api'),
    path('api/payments/<int:txn_id>/attach-obligation/', views.payment_attach_obligation_api, name='finance_payment_attach_obligation_api'),
    path('api/counterparties/<int:counterparty_id>/history/', views.counterparty_history_api, name='finance_counterparty_history_api'),

    # --- API: довідники + операції ---
    path('export/', views.payments_export, name='finance_payments_export'),
    path('api/dropdowns/', views.dropdowns_api, name='finance_dropdowns_api'),
    path('api/planned-totals/', views.planned_totals_api, name='finance_planned_totals_api'),
    path('api/entity/create/', views.quick_create_entity_api, name='finance_quick_create_entity_api'),
    path('api/transactions/create/', views.transaction_create_api, name='finance_txn_create_api'),
    path('api/transactions/bulk/', views.transactions_bulk_api, name='finance_txn_bulk_api'),
    path('api/transactions/<int:txn_id>/', views.transaction_detail_api, name='finance_txn_detail_api'),
    path('api/transactions/<int:txn_id>/update/', views.transaction_update_api, name='finance_txn_update_api'),
    path('api/transactions/<int:txn_id>/delete/', views.transaction_delete_api, name='finance_txn_delete_api'),
    path('api/transactions/<int:txn_id>/duplicate/', views.transaction_duplicate_api, name='finance_txn_duplicate_api'),
    path('api/transactions/<int:txn_id>/convert-transfer/', views.transaction_convert_transfer_api, name='finance_txn_convert_transfer_api'),
    path('api/transactions/<int:txn_id>/split/', views.transaction_split_api, name='finance_txn_split_api'),
    path('api/transactions/<int:txn_id>/mark-actual/', views.transaction_mark_actual_api, name='finance_txn_mark_actual_api'),
    path('api/transactions/<int:txn_id>/settle/', views.transaction_settle_api, name='finance_txn_settle_api'),
    path('api/recurrence/<int:rule_id>/update/', views.recurrence_update_api, name='finance_recurrence_update_api'),
    path('api/recurrence/<int:rule_id>/stop/', views.recurrence_stop_api, name='finance_recurrence_stop_api'),

    # --- API: рахунки ---
    path('api/accounts/create/', views.account_create_api, name='finance_account_create_api'),
    path('api/accounts/reorder/', views.accounts_reorder_api, name='finance_accounts_reorder_api'),
    path('api/accounts/<int:account_id>/update/', views.account_update_api, name='finance_account_update_api'),
    path('api/accounts/<int:account_id>/archive/', views.account_archive_api, name='finance_account_archive_api'),
    path('api/accounts/<int:account_id>/delete/', views.account_delete_api, name='finance_account_delete_api'),
    path('api/accounts/<int:account_id>/correct/', views.account_correct_balance_api, name='finance_account_correct_api'),

    # --- API: інтеграції ---
    path('api/integrations/providers/', views.integration_providers_api, name='finance_integration_providers_api'),
    path('api/integrations/start/', views.integration_start_api, name='finance_integration_start_api'),
    path('api/integrations/<int:conn_id>/status/', views.integration_status_api, name='finance_integration_status_api'),
    path('api/integrations/<int:conn_id>/refresh-qr/', views.integration_refresh_qr_api, name='finance_integration_refresh_qr_api'),
    path('api/integrations/<int:conn_id>/link/', views.integration_link_api, name='finance_integration_link_api'),
    path('api/integrations/<int:conn_id>/cancel/', views.integration_cancel_api, name='finance_integration_cancel_api'),

    # --- API: Monobank (підключення за токеном) ---
    path('api/integrations/mono/connect/', views.mono_connect_api, name='finance_mono_connect_api'),
    path('api/integrations/mono/connections/', views.mono_connections_api, name='finance_mono_connections_api'),
    path('api/integrations/mono/<int:conn_id>/accounts/', views.mono_accounts_api, name='finance_mono_accounts_api'),
    path('api/integrations/mono/<int:conn_id>/link/', views.mono_link_api, name='finance_mono_link_api'),
    path('api/integrations/mono/<int:conn_id>/sync/', views.mono_sync_api, name='finance_mono_sync_api'),
    path('api/integrations/mono/<int:conn_id>/disconnect/', views.mono_disconnect_api, name='finance_mono_disconnect_api'),
    path('api/integrations/mono/account/<int:account_id>/settings/', views.mono_account_settings_api, name='finance_mono_account_settings_api'),
    # Вебхук: секрет у шляху автентифікує виклик (без сесії/CSRF).
    path('hooks/mono/<int:conn_id>/<str:secret>/', views.mono_webhook, name='finance_mono_webhook'),

    # --- API: налаштування та push-повідомлення ---
    path('api/settings/get/', views.settings_get_api, name='finance_settings_get_api'),
    path('api/settings/save/', views.settings_save_api, name='finance_settings_save_api'),
    path('api/push/subscribe/', views.push_subscribe_api, name='finance_push_subscribe_api'),
    path('api/push/unsubscribe/', views.push_unsubscribe_api, name='finance_push_unsubscribe_api'),
    path('api/push/test/', views.push_test_api, name='finance_push_test_api'),
    path('api/notifications/history/', views.notification_history_api, name='finance_notification_history_api'),
    path('api/notifications/<int:log_id>/', views.notification_detail_api, name='finance_notification_detail_api'),
    path('api/notifications/<int:log_id>/ack/', views.notification_ack_api, name='finance_notification_ack_api'),

    # --- API: імпорт виписки ---
    path('api/import/preview/', views.import_preview_api, name='finance_import_preview_api'),
    path('api/import/confirm/', views.import_confirm_api, name='finance_import_confirm_api'),

    # --- API: календар ---
    path('api/calendar/day/<str:date_str>/', views.calendar_day_api, name='finance_calendar_day_api'),

    # Service worker (з кореня субдомену — щоб scope '/' працював для PWA).
    path('finance-sw.js', views.finance_service_worker, name='finance_sw'),

    # Журнал платежів — головна
    path('', views.payments, name='finance_home'),
]
