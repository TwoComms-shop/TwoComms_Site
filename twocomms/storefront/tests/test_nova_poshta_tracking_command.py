from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings


class NovaPoshtaTrackingCommandTests(TestCase):
    @override_settings(NOVA_POSHTA_API_KEY="")
    def test_missing_api_key_exits_with_command_error(self):
        with self.assertRaisesRegex(CommandError, "NOVA_POSHTA_API_KEY"):
            call_command("update_tracking_statuses", stdout=StringIO())

    @override_settings(NOVA_POSHTA_API_KEY="test-key")
    @patch("orders.management.commands.update_tracking_statuses.NovaPoshtaService")
    def test_provider_batch_errors_are_deferred_without_command_error(self, service_cls):
        service = service_cls.return_value
        queryset = MagicMock()
        queryset.count.return_value = 2
        service.get_orders_with_tracking_queryset.return_value = queryset
        service.update_all_tracking_statuses.return_value = {
            "total_orders": 2, "processed": 2, "updated": 1, "errors": 1,
            "provider_errors": 1, "transient_provider_errors": 1,
            "fatal_provider_errors": 0, "row_errors": 0, "application_errors": 0,
        }

        call_command("update_tracking_statuses", stdout=StringIO())

    @override_settings(NOVA_POSHTA_API_KEY="test-key")
    @patch("orders.management.commands.update_tracking_statuses.task_heartbeat")
    @patch("orders.management.commands.update_tracking_statuses.NovaPoshtaService")
    def test_row_errors_mark_provider_degraded_without_command_error(
        self, service_cls, task_heartbeat,
    ):
        service = service_cls.return_value
        queryset = MagicMock()
        queryset.count.return_value = 1
        service.get_orders_with_tracking_queryset.return_value = queryset
        service.update_all_tracking_statuses.return_value = {
            "total_orders": 1, "processed": 1, "updated": 0, "errors": 1,
            "provider_errors": 0, "transient_provider_errors": 0,
            "fatal_provider_errors": 0, "row_errors": 1, "application_errors": 0,
        }

        call_command("update_tracking_statuses", stdout=StringIO())

        heartbeat_state = task_heartbeat.return_value.__enter__.return_value
        heartbeat_state.mark_degraded.assert_called_once_with("nova_poshta_provider_degraded")

    @override_settings(NOVA_POSHTA_API_KEY="test-key")
    @patch("orders.management.commands.update_tracking_statuses.NovaPoshtaService")
    def test_application_errors_exit_with_command_error(self, service_cls):
        service = service_cls.return_value
        queryset = MagicMock()
        queryset.count.return_value = 1
        service.get_orders_with_tracking_queryset.return_value = queryset
        service.update_all_tracking_statuses.return_value = {
            "total_orders": 1, "processed": 1, "updated": 0, "errors": 1,
            "provider_errors": 0, "transient_provider_errors": 0,
            "fatal_provider_errors": 0, "row_errors": 0, "application_errors": 1,
        }

        with self.assertRaisesRegex(CommandError, "application"):
            call_command("update_tracking_statuses", stdout=StringIO())

    @override_settings(NOVA_POSHTA_API_KEY="test-key")
    @patch("orders.management.commands.update_tracking_statuses.NovaPoshtaService")
    def test_fatal_provider_errors_exit_with_command_error(self, service_cls):
        service = service_cls.return_value
        queryset = MagicMock()
        queryset.count.return_value = 1
        service.get_orders_with_tracking_queryset.return_value = queryset
        service.update_all_tracking_statuses.return_value = {
            "total_orders": 1, "processed": 1, "updated": 0, "errors": 1,
            "provider_errors": 0, "transient_provider_errors": 0,
            "fatal_provider_errors": 1, "row_errors": 0, "application_errors": 0,
        }

        with self.assertRaisesRegex(CommandError, "fatal provider"):
            call_command("update_tracking_statuses", stdout=StringIO())

    @override_settings(NOVA_POSHTA_API_KEY="test-key")
    @patch("orders.management.commands.update_tracking_statuses.NovaPoshtaService")
    def test_unclassified_aggregate_errors_exit_with_command_error(self, service_cls):
        service = service_cls.return_value
        queryset = MagicMock()
        queryset.count.return_value = 1
        service.get_orders_with_tracking_queryset.return_value = queryset
        service.update_all_tracking_statuses.return_value = {
            "total_orders": 1, "processed": 1, "updated": 0, "errors": 1,
        }

        with self.assertRaisesRegex(CommandError, "unclassified"):
            call_command("update_tracking_statuses", stdout=StringIO())

    @override_settings(NOVA_POSHTA_API_KEY="test-key")
    @patch("orders.management.commands.update_tracking_statuses.task_heartbeat")
    @patch("orders.management.commands.update_tracking_statuses.NovaPoshtaService")
    def test_no_work_run_preserves_existing_heartbeat_outcome(
        self, service_cls, task_heartbeat,
    ):
        service = service_cls.return_value
        queryset = MagicMock()
        queryset.count.return_value = 0
        service.get_orders_with_tracking_queryset.return_value = queryset
        heartbeat_state = task_heartbeat.return_value.__enter__.return_value

        call_command("update_tracking_statuses", stdout=StringIO())

        heartbeat_state.mark_skipped.assert_called_once_with()
        service.update_all_tracking_statuses.assert_not_called()

    @override_settings(NOVA_POSHTA_API_KEY="test-key")
    @patch("orders.management.commands.update_tracking_statuses.NovaPoshtaService")
    def test_clean_batch_returns_successfully(self, service_cls):
        service = service_cls.return_value
        queryset = MagicMock()
        queryset.count.return_value = 1
        service.get_orders_with_tracking_queryset.return_value = queryset
        service.update_all_tracking_statuses.return_value = {
            "total_orders": 1, "processed": 1, "updated": 0, "errors": 0,
        }
        stdout = StringIO()

        call_command("update_tracking_statuses", stdout=stdout)

        self.assertIn("Ошибок: 0", stdout.getvalue())
