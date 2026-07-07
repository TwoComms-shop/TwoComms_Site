# CB-015: Карта management-команд → где вызывается → вердикт

Дата: 07.07.2026. Метод: полнотекстовый поиск по репо (`call_command('cmd'` / `manage.py cmd`) по *.py, *.sh, *.md, *.txt.
**Ограничение:** crontab сервера недоступен (SSH-порт фильтруется из sandbox) — команды без code-refs могут вызываться из cron. Финальный вердикт «удалять» — ТОЛЬКО после сверки с `crontab -l` на сервере.

Всего команд: **93**.

## Легенда вердиктов
- **RUNTIME** — вызывается из кода приложения (signals/services/templatetags) → трогать нельзя
- **TESTED** — есть тесты, ручная/операционная команда → оставить
- **CRON?** — по названию/назначению явно периодическая, code-refs нет → сверить с crontab, вероятно живая
- **OPS** — разовая ремонтная/сервисная команда, refs нет → оставить как инструмент, но задокументировать
- **DEAD?** — кандидат на удаление (нет refs, нет очевидного периодического назначения, demo/test-природа)

## Кандидаты на удаление (DEAD?) — подтвердить crontab'ом
| Команда | Приложение | Обоснование |
|---|---|---|
| finance_seed_demo | finance | demo-сидер, refs=0 |
| notify_test_shops | management | «test» в названии, refs=0 |
| send_storage_test | warehouse | тестовая отправка, refs=0 |
| send_test_receipt | orders | тестовый чек, refs=0 |
| collectstatic (dtf) | dtf | **теневой оверрайд стандартной django-команды** — refs только в доках Ideas/Promt; проверить, не ломает ли деплой |

## RUNTIME (вызываются из кода — не трогать)
| Команда | Вызывается из |
|---|---|
| regenerate_feeds_if_dirty | storefront/services/feeds_queue.py, storefront/signals.py |
| optimize_images | storefront/templatetags/responsive_images.py |
| reindex_indexnow | storefront/management/commands/normalize_slugs.py |
| import_product_translations | data/build_product_translations.py |

## TESTED (покрыты тестами — операционные, живые)
finance_mono_sync, finance_repair_balances, compute_nightly_scores, generate_bot_fingerprints, generate_weekly_reviews, parser_recovery_dry_run, poll_ig_deal_payments, poll_wholesale_invoice_payments, process_telephony_webhooks, purge_ig_clients, recalculate_visible_points, reconcile_call_records, refresh_dtf_bridge_snapshot, release_frozen_commissions, seed_management_defaults, send_management_reminders, audit_product_images, autofill_product_seo, backfill_default_color, ensure_default_size_catalogs, generate_buyme_feed, generate_google_merchant_feed, generate_kasta_feed, generate_prom_feed, generate_rozetka_feed, publish_custom_print_blog, recraft_product_seo

## CRON? (периодические по назначению, refs=0 — сверить с crontab)
finance_generate_recurring, finance_send_reports, checker_tick, run_call_ai_analyses, run_instagram_bot, update_tracking_statuses, check_survey_inactivity, generate_instagram_feed, generate_sitemap, refresh_external_analytics, refresh_product_faqs, send_utm_report, submit_indexnow_urls, trim_analytics, send_storage_reminder (refs: warehouse/README), poll-команды

## OPS (ремонтные/разовые, refs=0 — оставить, задокументировать)
create_missing_points, merge_split_accounts, setup_telegram_webhook, finance_categorize_mcc, finance_fix_hold_drafts, finance_reconcile_transfers, finance_repair_wrong_transfers, backfill_management_v7_analytics, backfill_management_website_match_keys, checker_backfill_networks, checker_calibrate, clear_dropshipper_orders, fix_delivered_orders, generate_order_numbers, recover_checkouts, set_telegram_config, audit_translations, check_translation_coverage, compress_media_originals, convert_originals_to_webp, enable_slow_query_log, enqueue_optimize_images, fix_seo_text_data, fix_site_domain, generate_ai_content, generate_alt_texts, generate_promo_codes, generate_seo_meta, generate_web_push_vapid_keys, generate_wholesale_prices, inject_sales_seo_h2, normalize_slugs, prune_orphan_media, repoint_stale_image_fields, resend_custom_print_notification, seed_color_landings, setup_main_telegram_webhook, translate_products, seed_warehouse_admin (README), setup_storage_webhook (README), minify_dtf_assets (доки Ideas), check_duplicate_queue (доки)

## SSH-остаток
```bash
crontab -l   # сверить CRON?-группу; всё из DEAD?, чего нет в cron → удалять
```
