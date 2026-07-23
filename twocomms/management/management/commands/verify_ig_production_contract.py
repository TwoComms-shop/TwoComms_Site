"""Fail-closed Instagram CRM verification against the explicitly named production DB."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from management.models import IgBotNotification
from management.services import instagram_bot as bot
from management.services.ig_maintenance import maintenance_status


def _normalized_database_name(value) -> str:
    return str(value or "").strip()


def _assert_production_database(expected_database: str) -> dict:
    expected = _normalized_database_name(expected_database)
    actual = _normalized_database_name(connection.settings_dict.get("NAME"))
    vendor = str(connection.vendor or "")
    if not expected:
        raise CommandError("--expected-database is required")
    if expected.lower().startswith("test_") or actual.lower().startswith("test_"):
        raise CommandError("test database is forbidden for production verification")
    if vendor != "mysql":
        raise CommandError(f"production verification requires MySQL/MariaDB, got {vendor!r}")
    if actual != expected:
        raise CommandError(f"database identity mismatch: expected {expected!r}, got {actual!r}")
    with connection.cursor() as cursor:
        cursor.execute("SELECT DATABASE()")
        selected = _normalized_database_name(cursor.fetchone()[0])
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.schemata "
            "WHERE schema_name LIKE 'test\\_%' ESCAPE '\\\\'"
        )
        test_database_count = int(cursor.fetchone()[0])
    if selected != expected:
        raise CommandError(
            f"server-selected database mismatch: expected {expected!r}, got {selected!r}"
        )
    if test_database_count:
        raise CommandError(f"test database schemas detected: {test_database_count}")
    return {
        "vendor": vendor,
        "database": actual,
        "selected_database": selected,
        "test_database_count": test_database_count,
    }


def _run_rollback_fixtures() -> dict:
    maintenance = maintenance_status()
    if not maintenance.get("active"):
        raise CommandError("--rollback-fixtures requires an active maintenance lease")
    prefix = f"prod_contract_{uuid.uuid4().hex}_"
    fixture_ids = [-int(uuid.uuid4().int % 1_000_000_000) - offset for offset in (1, 2, 3)]
    if IgBotNotification.objects.filter(pk__in=fixture_ids).exists():
        raise CommandError("negative fixture ID collision")
    auto_increment_before = _notification_auto_increment()
    failure_rollback = _prove_mid_fixture_failure_rollback(
        prefix=prefix + "failure_",
        fixture_id=-int(uuid.uuid4().int % 1_000_000_000) - 10,
        expected_auto_increment=auto_increment_before,
    )
    outer = transaction.atomic()
    outer.__enter__()
    try:
        sent = IgBotNotification.objects.create(
            id=fixture_ids[0],
            dedupe_key=prefix + "sent",
            payload={"text": "mocked production contract", "chat_id": "123"},
        )
        unknown = IgBotNotification.objects.create(
            id=fixture_ids[1],
            dedupe_key=prefix + "unknown",
            payload={"text": "mocked production timeout", "chat_id": "123"},
        )
        dead = IgBotNotification.objects.create(
            id=fixture_ids[2],
            dedupe_key=prefix + "dead",
            payload={"text": "mocked production dead letter", "chat_id": "123"},
            status=IgBotNotification.Status.FAILED,
            attempts=4,
            next_attempt_at=timezone.now() - timedelta(seconds=1),
        )
        environment = {
            "MANAGEMENT_TG_BOT_TOKEN": "no-network-contract-token",
            "MANAGEMENT_TG_ADMIN_CHAT_ID": "123",
        }
        with (
            patch.dict("os.environ", environment, clear=False),
            patch(
                "management.services.ig_maintenance.notification_send_boundary",
                _always_allow_mocked_send,
            ),
        ):
            with patch(
                "management.services.instagram_bot._http",
                return_value=(200, json.dumps({"ok": True, "result": {"message_id": 9100}})),
            ) as http:
                if not bot._deliver_manager_notification(sent.dedupe_key):
                    raise CommandError("mocked sent fixture did not reach sent")
                http.assert_called_once()
            with patch(
                "management.services.instagram_bot._http",
                return_value=(-1, "mocked timeout"),
            ) as http:
                if bot._deliver_manager_notification(unknown.dedupe_key):
                    raise CommandError("mocked timeout fixture unexpectedly sent")
                http.assert_called_once()
            with patch(
                "management.services.instagram_bot._http",
                return_value=(503, json.dumps({"ok": False, "description": "mocked"})),
            ) as http:
                bot._deliver_manager_notification(dead.dedupe_key)
                http.assert_called_once()
        sent.refresh_from_db()
        unknown.refresh_from_db()
        dead.refresh_from_db()
        result = {
            "sent": {"status": sent.status, "attempts": sent.attempts},
            "unknown": {"status": unknown.status, "attempts": unknown.attempts},
            "dead": {"status": dead.status, "attempts": dead.attempts},
            "transport": "mocked_no_network",
            "mid_fixture_failure_rollback": failure_rollback,
        }
        if result != {
            "sent": {"status": IgBotNotification.Status.SENT, "attempts": 1},
            "unknown": {"status": IgBotNotification.Status.UNKNOWN, "attempts": 1},
            "dead": {"status": IgBotNotification.Status.DEAD_LETTER, "attempts": 5},
            "transport": "mocked_no_network",
            "mid_fixture_failure_rollback": "proven",
        }:
            raise CommandError(f"rollback fixture contract failed: {result!r}")
        return result
    finally:
        transaction.set_rollback(True)
        outer.__exit__(None, None, None)
        if IgBotNotification.objects.filter(dedupe_key__startswith=prefix).exists():
            raise CommandError("rollback fixture leaked rows")
        auto_increment_after = _notification_auto_increment()
        if auto_increment_after != auto_increment_before:
            raise CommandError(
                "rollback fixture changed AUTO_INCREMENT: "
                f"before={auto_increment_before!r}, after={auto_increment_after!r}"
            )


def _notification_auto_increment():
    table_name = IgBotNotification._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT AUTO_INCREMENT FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            [table_name],
        )
        row = cursor.fetchone()
    if not row:
        raise CommandError(f"notification table not found: {table_name}")
    return row[0]


def _prove_mid_fixture_failure_rollback(
    *, prefix: str, fixture_id: int, expected_auto_increment
) -> str:
    try:
        with transaction.atomic():
            IgBotNotification.objects.create(
                id=fixture_id,
                dedupe_key=prefix + "row",
                payload={"text": "forced rollback", "chat_id": "123"},
            )
            raise RuntimeError("intentional rollback proof")
    except RuntimeError as exc:
        if str(exc) != "intentional rollback proof":
            raise
    if IgBotNotification.objects.filter(dedupe_key__startswith=prefix).exists():
        raise CommandError("mid-fixture exception leaked rows")
    if _notification_auto_increment() != expected_auto_increment:
        raise CommandError("mid-fixture exception changed AUTO_INCREMENT")
    return "proven"


class _always_allow_mocked_send:
    def __enter__(self):
        return True

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class Command(BaseCommand):
    help = "Перевірити IG CRM тільки проти явно названої production MariaDB."

    def add_arguments(self, parser):
        parser.add_argument("--expected-database", required=True)
        parser.add_argument("--rollback-fixtures", action="store_true")

    def handle(self, *args, **options):
        database = _assert_production_database(options["expected_database"])
        result = {
            "ok": True,
            "read_only": not options["rollback_fixtures"],
            "database_contract": database,
            "rollback_fixtures": None,
        }
        if options["rollback_fixtures"]:
            result["rollback_fixtures"] = _run_rollback_fixtures()
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
