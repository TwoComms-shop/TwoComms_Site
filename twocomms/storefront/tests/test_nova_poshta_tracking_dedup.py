"""
Тесты дедупликации Telegram-уведомлений об изменении статуса посылки НП.

Регресс: при «забрали» (StatusCode=9) НП может несколько раз менять
свободный текст Status/StatusDescription (плата за зберігання, грошові
перекази, таймстемпи). Раньше каждое такое изменение текста слало
повторное «ОНОВЛЕННЯ СТАТУСУ ПОСИЛКИ». Теперь уведомление шлётся
только при смене именно StatusCode.
"""
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from orders.models import Order
from orders.nova_poshta_service import NovaPoshtaAPIError, NovaPoshtaService
from storefront.models import UserAction


def _tracking(status, code, description=""):
    return {
        "Number": "20451234123456",
        "Status": status,
        "StatusCode": code,
        "StatusDescription": description,
    }


def _order_kwargs(number):
    return {
        "order_number": number,
        "full_name": "Тест Клієнт",
        "phone": "+380991112233",
        "city": "Київ",
        "np_office": "Відділення №4",
        "total_sum": Decimal("1499.00"),
        "status": "ship",
        "payment_status": "unpaid",
        "tracking_number": number,
    }


@override_settings(NOVA_POSHTA_API_KEY="test-key")
class NovaPoshtaTrackingDedupTests(TestCase):
    def setUp(self):
        self.order = Order.objects.create(
            order_number="TESTNP001",
            full_name="Тест Клієнт",
            phone="+380991112233",
            city="Київ",
            np_office="Відділення №4",
            total_sum=Decimal("1499.00"),
            status="ship",
            payment_status="unpaid",
            tracking_number="20451234123456",
        )
        self.service = NovaPoshtaService()

    def _run(self, tracking_info):
        """Прогон одного цикла обновления с заданным ответом API."""
        with (
            patch.object(self.service, "get_tracking_info", return_value=tracking_info),
            patch.object(self.service, "_send_status_notification") as status_notif,
            patch.object(self.service, "_send_delivery_notification") as delivery_notif,
            patch.object(self.service, "_send_admin_delivery_notification") as admin_notif,
            patch.object(self.service, "_send_facebook_purchase_event"),
        ):
            result = self.service.update_order_tracking_status(self.order)
        self.order.refresh_from_db()
        return result, status_notif, delivery_notif, admin_notif

    def test_same_code_changing_text_does_not_resend(self):
        # Первое появление кода 4 — уведомление есть
        _, status_notif, _, _ = self._run(
            _tracking("Прибув на відділення", 4, "очікує отримувача")
        )
        self.assertEqual(status_notif.call_count, 1)

        # Тот же код 4, но другой текст (плата за зберігання) — НЕ слать
        _, status_notif, _, _ = self._run(
            _tracking("Прибув на відділення", 4, "платне зберігання, 1 доба")
        )
        self.assertEqual(status_notif.call_count, 0)

        # Ещё раз другой текст, тот же код — снова молчим
        _, status_notif, _, _ = self._run(
            _tracking("Прибув на відділення", 4, "платне зберігання, 2 доби")
        )
        self.assertEqual(status_notif.call_count, 0)

    def test_code_change_triggers_single_notification_each(self):
        _, status_notif, _, _ = self._run(_tracking("Відправлено", 2))
        self.assertEqual(status_notif.call_count, 1)

        _, status_notif, _, _ = self._run(_tracking("Прибув на відділення", 4))
        self.assertEqual(status_notif.call_count, 1)

    def test_received_sends_delivery_once_and_no_spam_after(self):
        # Доставка: код 9 -> заказ done, уведомление о доставке один раз
        _, status_notif, delivery_notif, admin_notif = self._run(
            _tracking("Відправлення отримано", 9, "одержувачем")
        )
        self.assertEqual(self.order.status, "done")
        self.assertEqual(self.order.payment_status, "paid")
        self.assertEqual(delivery_notif.call_count, 1)
        self.assertEqual(status_notif.call_count, 0)
        self.assertEqual(admin_notif.call_count, 1)

        # Тот же код 9, но НП дописал инфу про грошовий переказ — НЕ спамим
        _, status_notif, delivery_notif, _ = self._run(
            _tracking("Відправлення отримано", 9, "грошовий переказ виплачено")
        )
        self.assertEqual(status_notif.call_count, 0)
        self.assertEqual(delivery_notif.call_count, 0)

    def test_codes_9_10_11_are_terminal_delivery_successes(self):
        for index, code in enumerate((9, 10, 11), start=1):
            with self.subTest(code=code):
                order = self.order if code == 9 else Order.objects.create(
                    **_order_kwargs(f"204512341234{index + 5:02d}")
                )
                with (
                    patch.object(self.service, "get_tracking_info", return_value=_tracking("Отримано", code)),
                    patch.object(self.service, "_send_admin_delivery_notification"),
                    patch.object(self.service, "_send_delivery_notification"),
                    patch.object(self.service, "_send_facebook_purchase_event"),
                    patch.object(self.service, "_send_tiktok_purchase_event"),
                ):
                    self.service.update_order_tracking_status(order)
                order.refresh_from_db()
                self.assertEqual(order.status, "done")
                self.assertEqual(order.tracking_status_code, code)

    def test_terminal_failure_is_not_polled_again(self):
        self.order.tracking_status_code = 103
        self.order.tracking_terminal_at = timezone.now()
        self.order.save(update_fields=["tracking_status_code", "tracking_terminal_at"])

        with patch.object(self.service, "get_tracking_info") as get_tracking:
            result = self.service.update_all_tracking_statuses()

        self.assertEqual(result["processed"], 0)
        get_tracking.assert_not_called()

    def test_batch_tracking_splits_101_orders_into_100_and_one(self):
        for index in range(2, 102):
            Order.objects.create(**_order_kwargs(f"2045123412{index:04d}"))

        numbers = list(Order.objects.filter(tracking_number__isnull=False).values_list("tracking_number", flat=True))
        calls = []

        def batch(documents):
            calls.append([item["DocumentNumber"] for item in documents])
            return {
                number: _tracking("Відправлено", 5)
                for number in calls[-1]
            }

        with (
            patch.object(self.service, "get_tracking_info_batch", side_effect=batch),
            patch.object(self.service, "get_tracking_info", return_value=_tracking("Відправлено", 5)) as single,
            patch.object(self.service, "_send_status_notification"),
        ):
            result = self.service.update_all_tracking_statuses()

        self.assertEqual(result["processed"], 101)
        self.assertEqual([len(call) for call in calls], [100])
        self.assertEqual(single.call_count, 1)
        self.assertEqual(len({number for call in calls for number in call}) + single.call_count, 101)

    def test_failed_batch_counts_rows_as_processed_and_increments_failures(self):
        self.order.tracking_failure_count = 2
        self.order.save(update_fields=["tracking_failure_count"])

        with patch.object(
            self.service,
            "get_tracking_info_batch",
            side_effect=RuntimeError("provider unavailable"),
        ):
            result = self.service.update_all_tracking_statuses()

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["errors"], 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.tracking_failure_count, 3)

    def test_batch_response_is_matched_by_number_and_keeps_latest_duplicate(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "success": True,
            "data": [
                {"Number": "20451234999999", "StatusCode": 5, "DateLastMovementStatus": "2026-08-03 10:00:00"},
                {"Number": "20451234123456", "StatusCode": 4, "DateLastMovementStatus": "2026-08-03 09:00:00"},
                {"Number": "20451234123456", "StatusCode": 7, "DateLastMovementStatus": "2026-08-03 11:00:00"},
                {"Number": "99999999999999", "StatusCode": 9},
            ],
        }
        session = Mock()
        session.post.return_value = response

        with patch("orders.nova_poshta_service.requests.Session", return_value=session):
            result = self.service.get_tracking_info_batch(
                [
                    {"DocumentNumber": "20451234 123456"},
                    {"DocumentNumber": "20451234999999"},
                    {"DocumentNumber": "20451234000000"},
                ]
            )

        self.assertEqual(result["20451234123456"]["StatusCode"], 7)
        self.assertEqual(result["20451234999999"]["StatusCode"], 5)
        self.assertNotIn("20451234000000", result)
        self.assertNotIn("99999999999999", result)

    def test_delivery_lifecycle_is_emitted_before_telegram_failure(self):
        with (
            patch.object(
                self.service,
                "get_tracking_info",
                return_value=_tracking("Відправлення отримано", 9, "одержувачем"),
            ),
            patch.object(self.service, "_dispatch_ig_delivery_lifecycle") as lifecycle,
            patch.object(self.service, "_send_admin_delivery_notification"),
            patch.object(
                self.service,
                "_send_delivery_notification",
                side_effect=RuntimeError("telegram unavailable"),
            ),
            patch.object(self.service, "_send_facebook_purchase_event"),
            patch.object(self.service, "_send_tiktok_purchase_event"),
        ):
            with self.assertRaisesRegex(RuntimeError, "telegram unavailable"):
                self.service.update_order_tracking_status(self.order)

        lifecycle.assert_called_once()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "done")

    def test_lifecycle_failure_does_not_block_delivery_notifications(self):
        with (
            patch.object(
                self.service,
                "get_tracking_info",
                return_value=_tracking("Відправлення отримано", 9, "одержувачем"),
            ),
            patch(
                "management.services.ig_lifecycle.ensure_lifecycle_event",
                side_effect=RuntimeError("lifecycle unavailable"),
            ),
            patch.object(self.service, "_send_admin_delivery_notification") as admin_notification,
            patch.object(self.service, "_send_delivery_notification") as delivery_notification,
            patch.object(self.service, "_send_facebook_purchase_event"),
            patch.object(self.service, "_send_tiktok_purchase_event"),
        ):
            result = self.service.update_order_tracking_status(self.order)

        self.assertTrue(result)
        admin_notification.assert_called_once()
        delivery_notification.assert_called_once()

    def test_received_heals_purchase_when_order_was_already_paid(self):
        self.order.payment_status = 'paid'
        self.order.save(update_fields=['payment_status'])

        self._run(_tracking("Відправлення отримано", 9, "одержувачем"))

        self.assertEqual(
            UserAction.objects.filter(action_type='purchase', order_id=self.order.pk).count(),
            1,
        )

    def test_received_online_prepaid_order_does_not_emit_purchase_events(self):
        """Delivery completes fulfillment but is never an online conversion."""
        self.order.pay_type = 'prepay_200'
        self.order.payment_status = 'prepaid'
        self.order.payment_provider = 'monobank_pay'
        self.order.payment_invoice_id = 'invoice-42'
        self.order.payment_payload = {
            'post_payment_channels': {
                'instagram_lifecycle': {'state': 'pending'},
            },
        }
        self.order.save(update_fields=[
            'pay_type', 'payment_status', 'payment_provider',
            'payment_invoice_id', 'payment_payload',
        ])

        with (
            patch.object(
                self.service,
                'get_tracking_info',
                return_value=_tracking('Відправлення отримано', 9, 'одержувачем'),
            ),
            patch.object(self.service, '_send_admin_delivery_notification'),
            patch.object(self.service, '_send_delivery_notification'),
            patch.object(self.service, '_send_facebook_purchase_event') as facebook_purchase,
            patch.object(self.service, '_send_tiktok_purchase_event') as tiktok_purchase,
        ):
            self.service.update_order_tracking_status(self.order)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'done')
        self.assertEqual(self.order.payment_status, 'paid')
        facebook_purchase.assert_not_called()
        tiktok_purchase.assert_not_called()

    def test_received_manual_instagram_cod_order_does_not_emit_website_purchase(self):
        """Manual Instagram sales are not eligible for the website COD fallback."""
        self.order.source = 'manual'
        self.order.sale_source = 'Instagram'
        self.order.pay_type = 'cod'
        self.order.save(update_fields=['source', 'sale_source', 'pay_type'])

        with (
            patch.object(
                self.service,
                'get_tracking_info',
                return_value=_tracking('Відправлення отримано', 9, 'одержувачем'),
            ),
            patch.object(self.service, '_send_admin_delivery_notification'),
            patch.object(self.service, '_send_delivery_notification'),
            patch.object(self.service, '_send_facebook_purchase_event') as facebook_purchase,
            patch.object(self.service, '_send_tiktok_purchase_event') as tiktok_purchase,
        ):
            self.service.update_order_tracking_status(self.order)

        facebook_purchase.assert_not_called()
        tiktok_purchase.assert_not_called()

    def test_received_legacy_web_cod_order_keeps_explicit_purchase_fallback(self):
        """Only the historical website COD flow can create a delivery Purchase."""
        self.order.pay_type = 'cod'
        self.order.source = 'web'
        self.order.save(update_fields=['pay_type', 'source'])

        with (
            patch.object(
                self.service,
                'get_tracking_info',
                return_value=_tracking('Відправлення отримано', 9, 'одержувачем'),
            ),
            patch.object(self.service, '_send_admin_delivery_notification'),
            patch.object(self.service, '_send_delivery_notification'),
            patch.object(self.service, '_send_facebook_purchase_event') as facebook_purchase,
            patch.object(self.service, '_send_tiktok_purchase_event') as tiktok_purchase,
        ):
            self.service.update_order_tracking_status(self.order)

        facebook_purchase.assert_called_once_with(self.order)
        tiktok_purchase.assert_called_once_with(self.order)

    def test_received_legacy_cod_with_payment_purchase_marker_is_not_reemitted(self):
        self.order.pay_type = 'cod'
        self.order.source = 'web'
        self.order.payment_payload = {
            'fb_conversions_api': {
                'event_name': 'Purchase',
                'event_id': 'TESTNP001_purchase',
            },
        }
        self.order.save(update_fields=['pay_type', 'source', 'payment_payload'])

        with (
            patch.object(
                self.service,
                'get_tracking_info',
                return_value=_tracking('Відправлення отримано', 9, 'одержувачем'),
            ),
            patch.object(self.service, '_send_admin_delivery_notification'),
            patch.object(self.service, '_send_delivery_notification'),
            patch.object(self.service, '_send_facebook_purchase_event') as facebook_purchase,
            patch.object(self.service, '_send_tiktok_purchase_event') as tiktok_purchase,
        ):
            self.service.update_order_tracking_status(self.order)

        facebook_purchase.assert_not_called()
        tiktok_purchase.assert_not_called()

    def test_repeated_received_poll_heals_done_order_missing_purchase(self):
        self.order.status = 'done'
        self.order.payment_status = 'paid'
        self.order.shipment_status = 'Відправлення отримано - одержувачем'
        self.order.payment_payload = {
            'np_tracking': {
                'last_status_code': 9,
                'last_status_text': self.order.shipment_status,
            },
        }
        self.order.save(update_fields=[
            'status',
            'payment_status',
            'shipment_status',
            'payment_payload',
        ])

        self._run(_tracking('Відправлення отримано', 9, 'одержувачем'))

        self.assertEqual(
            UserAction.objects.filter(action_type='purchase', order_id=self.order.pk).count(),
            1,
        )

    def test_bulk_scan_retries_done_order_until_purchase_is_healed(self):
        self.order.status = 'done'
        self.order.payment_status = 'paid'
        self.order.source = 'manual'
        self.order.shipment_status = 'Відправлення отримано - одержувачем'
        self.order.payment_payload = {
            'manual_payment_preset': 'cod',
            'np_tracking': {
                'last_status_code': 9,
                'last_status_text': self.order.shipment_status,
            },
        }
        self.order.save(update_fields=[
            'status',
            'payment_status',
            'source',
            'shipment_status',
            'payment_payload',
        ])

        with (
            patch.object(
                self.service,
                'get_tracking_info',
                return_value=_tracking('Відправлення отримано', 9, 'одержувачем'),
            ),
            patch.object(self.service, '_send_status_notification'),
            patch.object(self.service, '_send_delivery_notification'),
            patch.object(self.service, '_send_admin_delivery_notification'),
            patch.object(self.service, '_send_facebook_purchase_event'),
        ):
            first = self.service.update_all_tracking_statuses()
            second = self.service.update_all_tracking_statuses()

        self.assertEqual(first['processed'], 1)
        self.assertEqual(second['processed'], 0)
        self.assertEqual(
            UserAction.objects.filter(action_type='purchase', order_id=self.order.pk).count(),
            1,
        )

    def test_bulk_scan_excludes_ambiguous_legacy_and_free_done_orders(self):
        self.order.status = 'done'
        self.order.payment_status = 'paid'
        self.order.source = 'manual'
        self.order.shipment_status = 'Відправлення отримано - одержувачем'
        self.order.payment_payload = {
            'np_tracking': {'last_status_code': 9},
        }
        self.order.save(update_fields=[
            'status',
            'payment_status',
            'source',
            'shipment_status',
            'payment_payload',
        ])
        free_order = Order.objects.create(
            order_number='TESTNPFREE',
            full_name='Подарунок',
            phone='+380991112244',
            city='Київ',
            np_office='Відділення №4',
            total_sum=Decimal('0.00'),
            status='done',
            payment_status='paid',
            source='manual',
            tracking_number='20451234123457',
            shipment_status='Відправлення отримано - одержувачем',
            payment_payload={
                'manual_payment_preset': 'free',
                'np_tracking': {'last_status_code': 9},
            },
        )

        with patch.object(self.service, 'get_tracking_info') as get_tracking:
            result = self.service.update_all_tracking_statuses()

        self.assertEqual(result['processed'], 0)
        get_tracking.assert_not_called()
        self.assertFalse(
            UserAction.objects.filter(
                action_type='purchase',
                order_id__in=(self.order.pk, free_order.pk),
            ).exists()
        )

    def test_bulk_scan_excludes_done_order_with_non_delivery_provider_status(self):
        self.order.status = 'done'
        self.order.payment_status = 'paid'
        self.order.shipment_status = 'Номер не знайдено'
        self.order.payment_payload = {
            'np_tracking': {'last_status_code': 3},
        }
        self.order.save(update_fields=[
            'status',
            'payment_status',
            'shipment_status',
            'payment_payload',
        ])

        with patch.object(self.service, 'get_tracking_info') as get_tracking:
            result = self.service.update_all_tracking_statuses()

        self.assertEqual(result['processed'], 0)
        get_tracking.assert_not_called()

    def test_long_status_text_is_truncated_to_field_limit(self):
        long_desc = "д" * 300
        self._run(_tracking("Прибув на відділення", 4, long_desc))
        self.assertLessEqual(
            len(self.order.shipment_status or ""),
            NovaPoshtaService.SHIPMENT_STATUS_MAX_LENGTH,
        )

    def test_missing_status_code_falls_back_to_text(self):
        # Код не пришёл — детекция по тексту, одно уведомление
        _, status_notif, _, _ = self._run(_tracking("Прямує до відділення", None))
        self.assertEqual(status_notif.call_count, 1)

        # Тот же текст — без уведомления
        _, status_notif, _, _ = self._run(_tracking("Прямує до відділення", None))
        self.assertEqual(status_notif.call_count, 0)

    def test_delivery_is_idempotent_across_repeated_scans(self):
        """
        Имитация двух последовательных проходов update_all (как два worker'а
        Passenger друг за другом): доставка должна нотифицироваться РОВНО один
        раз суммарно, без повторного "АВТОМАТИЧНЕ ОНОВЛЕННЯ".
        """
        total_admin = 0
        total_delivery = 0
        for _ in range(3):
            _, _, delivery_notif, admin_notif = self._run(
                _tracking("Відправлення отримано", 9, "одержувачем")
            )
            total_admin += admin_notif.call_count
            total_delivery += delivery_notif.call_count

        self.assertEqual(total_admin, 1)
        self.assertEqual(total_delivery, 1)
        self.assertEqual(self.order.status, "done")

    def test_apply_update_runs_in_transaction_with_row_lock(self):
        """Гарантируем, что обновление берёт row-lock внутри транзакции."""
        captured = {}
        original = Order.objects.select_for_update

        def _spy(*args, **kwargs):
            captured["called"] = True
            return original(*args, **kwargs)

        with (
            patch.object(self.service, "get_tracking_info",
                         return_value=_tracking("Прибув на відділення", 4)),
            patch.object(self.service, "_send_status_notification"),
            patch.object(Order.objects, "select_for_update", side_effect=_spy),
        ):
            self.service.update_order_tracking_status(self.order)

        self.assertTrue(captured.get("called"))

    def test_tracking_poll_includes_saved_recipient_phone(self):
        with (
            patch.object(
                self.service,
                "get_tracking_info",
                return_value=_tracking("Прибув на відділення", 4),
            ) as get_tracking,
            patch.object(self.service, "_send_status_notification"),
        ):
            self.service.update_order_tracking_status(self.order)

        get_tracking.assert_called_once_with(
            self.order.tracking_number,
            phone=self.order.phone,
        )

    def test_apply_update_error_is_counted_and_closes_old_connections(self):
        with (
            patch.object(
                self.service,
                "get_tracking_info",
                return_value=_tracking("Прибув на відділення", 4),
            ),
            patch.object(self.service, "_apply_tracking_update", side_effect=RuntimeError("db down")),
            patch("orders.nova_poshta_service.close_old_connections") as close_old,
        ):
            result = self.service.update_all_tracking_statuses()

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["application_errors"], 1)
        self.assertEqual(result["provider_errors"], 0)
        self.assertGreaterEqual(close_old.call_count, 1)

    def test_provider_batch_failure_is_categorized_for_degraded_retry(self):
        second = Order.objects.create(**_order_kwargs("20451234123457"))
        with patch.object(
            self.service,
            "get_tracking_info_batch",
            side_effect=NovaPoshtaAPIError("provider unavailable"),
        ):
            result = self.service.update_all_tracking_statuses()

        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["errors"], 2)
        self.assertEqual(result["provider_errors"], 2)
        self.assertEqual(result["application_errors"], 0)
        self.order.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.order.tracking_failure_count, 1)
        self.assertEqual(second.tracking_failure_count, 1)

    def test_failed_batch_does_not_publish_success_heartbeat(self):
        with (
            patch.object(self.service, "update_order_tracking_status", side_effect=RuntimeError("boom")),
            patch("orders.nova_poshta_service.cache.set") as cache_set,
        ):
            result = self.service.update_all_tracking_statuses()

        self.assertEqual(result["errors"], 1)
        self.assertFalse(
            any(call.args[0] == self.service.LAST_UPDATE_CACHE_KEY for call in cache_set.call_args_list),
            "a failed batch must not publish the successful-tracking heartbeat",
        )

    def test_facebook_purchase_save_error_does_not_fallback_to_full_save(self):
        self.order.pay_type = 'cod'
        self.order.source = 'web'
        self.order.save(update_fields=['pay_type', 'source'])
        fake_service = type("FakeFacebookService", (), {
            "enabled": True,
            "send_purchase_event": lambda self, order: True,
        })()

        with (
            patch(
                "orders.facebook_conversions_service.get_facebook_conversions_service",
                return_value=fake_service,
            ),
            patch.object(self.order, "save", side_effect=RuntimeError("db down")) as save_mock,
        ):
            self.service._send_facebook_purchase_event(self.order)

        self.assertEqual(save_mock.call_count, 1)
        self.assertEqual(save_mock.call_args.kwargs, {"update_fields": ["payment_payload"]})

    def test_tiktok_purchase_save_error_does_not_fallback_to_full_save(self):
        self.order.pay_type = 'cod'
        self.order.source = 'web'
        self.order.save(update_fields=['pay_type', 'source'])
        fake_service = type("FakeTikTokService", (), {
            "enabled": True,
            "send_purchase_event": lambda self, order: True,
        })()

        with (
            patch(
                "orders.tiktok_events_service.get_tiktok_events_service",
                return_value=fake_service,
            ),
            patch.object(self.order, "save", side_effect=RuntimeError("db down")) as save_mock,
        ):
            self.service._send_tiktok_purchase_event(self.order)

        self.assertEqual(save_mock.call_count, 1)
        self.assertEqual(save_mock.call_args.kwargs, {"update_fields": ["payment_payload"]})
