"""
Тесты дедупликации Telegram-уведомлений об изменении статуса посылки НП.

Регресс: при «забрали» (StatusCode=9) НП может несколько раз менять
свободный текст Status/StatusDescription (плата за зберігання, грошові
перекази, таймстемпи). Раньше каждое такое изменение текста слало
повторное «ОНОВЛЕННЯ СТАТУСУ ПОСИЛКИ». Теперь уведомление шлётся
только при смене именно StatusCode.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from orders.models import Order
from orders.nova_poshta_service import NovaPoshtaService


def _tracking(status, code, description=""):
    return {
        "Number": "20451234123456",
        "Status": status,
        "StatusCode": code,
        "StatusDescription": description,
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

    def test_facebook_purchase_save_error_does_not_fallback_to_full_save(self):
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
