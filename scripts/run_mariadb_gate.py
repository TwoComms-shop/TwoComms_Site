#!/usr/bin/env python3
"""Run the Instagram bot's disposable MariaDB acceptance gate.

The runner owns only a generated schema/user.  It never accepts a schema or
user name from the caller and never falls back to SQLite or the application
database.  ``external`` is the CI service-container mode; ``native`` is a
strict adapter for a locally provisioned MariaDB server.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from ipaddress import ip_address
from pathlib import Path
from typing import Callable, Mapping, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = "lifecycle"
SUITES = {
    "lifecycle": (
        "management.tests_ig_mariadb_lifecycle",
        "storefront.tests.test_mariadb_constraints.WebPushEndpointUniquenessTests",
    ),
    "checkout-concurrency": (
        "management.tests_ig_checkout_models."
        "IgCheckoutProposalConcurrencyTests."
        "test_concurrent_replacement_creation_serializes_on_deal",
    ),
    "follow-ugc-concurrency": (
        "management.tests_ig_mariadb_follow_ugc",
    ),
    "lease-concurrency": (
        "management.tests_ig_daemon_progress.MariaDbLeaseContractTests",
    ),
}
SAFE_ENV_NAMES = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "VIRTUAL_ENV",
    "SYSTEMROOT",
}
PROVIDER_ENV_PREFIXES = (
    "TELEGRAM_",
    "MANAGER_TG_",
    "MANAGEMENT_TG_",
    "META_",
    "GEMINI_",
    "OPENAI_",
    "FACEBOOK_",
)
PRODUCTION_ENV_NAMES = {
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME_DTF",
    "DB_USER_DTF",
    "DB_PASSWORD_DTF",
    "DB_HOST_DTF",
    "DB_PORT_DTF",
}
MAX_FAILURE_SUMMARY_CHARS = 2048
REVIEW_WRITE_FREEZE_MARKER_BYTES = b"review-write-freeze-v1\n"
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TEST_RESULT_RE = re.compile(
    r"^(?:Ran \d+ tests? in [0-9.]+s|"
    r"FAILED(?: \((?:failures|errors|skipped|expected failures|"
    r"unexpected successes)=\d+(?:, (?:failures|errors|skipped|"
    r"expected failures|unexpected successes)=\d+)*\))?|"
    r"OK(?: \((?:skipped|expected failures|unexpected successes)=\d+(?:, "
    r"(?:skipped|expected failures|unexpected successes)=\d+)*\))?)$"
)
_TEST_FAILURE_RE = re.compile(r"^(ERROR|FAIL):\s+.+$")
_TEST_FAILURE_RESULT_RE = re.compile(r"^FAILED(?:\s+.*)?$")
_EXCEPTION_RE = re.compile(
    r"^(?:(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Failure)):(?:\s.*)?$"
)
_EXCEPTION_KIND_PATTERNS = (
    (re.compile(r"\b(?:OperationalError|ProgrammingError|IntegrityError|DatabaseError)\b"), "database"),
    (re.compile(r"\b(?:ImproperlyConfigured|CommandError|ConfigurationError)\b"), "configuration"),
    (re.compile(r"\b(?:ImportError|ModuleNotFoundError)\b"), "import"),
    (re.compile(r"\b(?:TypeError|AttributeError|ValueError)\b"), "type"),
    (re.compile(r"\b(?:RuntimeError|OSError|FileNotFoundError)\b"), "runtime"),
)
_DATABASE_ERRNO_RE = re.compile(
    r"^(?:(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Failure)):\s*"
    r"\(([1-9]\d{0,4}),"
)
_DATABASE_CHECK_WARNING_RE = re.compile(
    r"^(?P<object>[^:]+): \((?P<check_id>[A-Za-z0-9.]+)\) "
)
DATABASE_CHECK_WARNING_ALLOWLIST = {}
DATABASE_CHECK_WARNING_EXPECTED = {
    (object_name, check_id): int(policy["max_count"])
    for (object_name, check_id), policy in DATABASE_CHECK_WARNING_ALLOWLIST.items()
}
_RELEASE_MIGRATION = "0156_ig_order_event_delivery_receipts"
_RELEASE_TABLE = "management_igordercustomerevent"
_FOLLOW_UGC_MIGRATION = "0166_ig_ugc_reward_lifecycle"
_GUEST_PROMO_MIGRATION = "0095_promocode_guest_ugc"
_FOLLOW_UGC_TABLES = (
    "management_igfollowcapabilitystate",
    "management_igfollowstate",
    "management_igfollowobservation",
    "management_igfollowrefreshjob",
    "management_igfollowctadecision",
    "management_igugcreward",
    "management_igugcevidenceassessment",
    "management_igugcrewardlifetime",
    "management_igugcrewarddelivery",
    "management_igugcrewardlifecyclejob",
    "management_igpaymentfollowpreparation",
    "storefront_promocodeguestusage",
)
_FOLLOW_UGC_UNIQUE_COLUMNS = {
    "management_igfollowcapabilitystate": {("singleton_key",)},
    "management_igfollowstate": {("client_id",)},
    "management_igfollowrefreshjob": {("client_id",)},
    "management_igfollowctadecision": {
        ("trigger_key",),
        ("episode_slot_key",),
        ("sent_scope_key",),
    },
    "management_igugcreward": {
        ("order_id",),
        ("evidence_fingerprint",),
        ("promo_code_id",),
        ("lifetime_slot_key",),
    },
    "management_igugcevidenceassessment": {
        ("provider_object_digest",),
        ("client_id", "source_message_id"),
    },
    "management_igugcrewardlifetime": {
        ("client_id",),
        ("identity_digest",),
        ("reward_id",),
    },
    "management_igugcrewarddelivery": {("reward_id",)},
    "management_igpaymentfollowpreparation": {("lifecycle_event_id",)},
    "storefront_promocodeguestusage": {
        ("promo_code_id",),
        ("reservation_key",),
    },
}
_FOLLOW_UGC_LIFECYCLE_JOB_INDEX_COLUMNS = {
    ("order_id",),
    ("client_id",),
    ("due_at",),
    ("created_at",),
    ("due_at", "id"),
}


class GateError(RuntimeError):
    """A failed gate, retaining both execution and cleanup errors."""

    def __init__(
        self,
        message: str,
        *,
        primary_error: BaseException | None = None,
        cleanup_error: BaseException | None = None,
        cleanup_errors: list[BaseException] | tuple[BaseException, ...] = (),
    ):
        self.cleanup_errors = tuple(
            cleanup_errors or (() if cleanup_error is None else (cleanup_error,))
        )
        self.cleanup_error = cleanup_error or (self.cleanup_errors[0] if self.cleanup_errors else None)
        if self.cleanup_errors:
            message = f"{message}: cleanup_error=exception"
        super().__init__(message)
        self.primary_error = primary_error


def classify_database_check_warnings(
    output: str,
) -> dict[str, object]:
    counts: dict[tuple[str, str], int] = {}
    for line in output.splitlines():
        match = _DATABASE_CHECK_WARNING_RE.match(line.strip())
        if match is None:
            continue
        key = (match.group("object"), match.group("check_id"))
        counts[key] = counts.get(key, 0) + 1

    allowed_count = 0
    blocked = []
    for key, count in sorted(counts.items()):
        policy = DATABASE_CHECK_WARNING_ALLOWLIST.get(key)
        rendered = f"{key[0]}:{key[1]}"
        if policy is None:
            blocked.append(rendered)
            continue
        if count > int(policy["max_count"]):
            blocked.append(rendered)
            continue
        allowed_count += count
    missing = [
        f"{key[0]}:{key[1]}"
        for key, expected_count in sorted(DATABASE_CHECK_WARNING_EXPECTED.items())
        if counts.get(key, 0) != expected_count
    ]
    return {
        "allowed_count": allowed_count,
        "blocked": blocked,
        "missing": missing,
        "policies": DATABASE_CHECK_WARNING_ALLOWLIST,
    }


def _failure_summary(*, suite: str, completed: subprocess.CompletedProcess) -> str:
    lines = [
        f"MariaDB gate child failed: suite={suite} exit={completed.returncode}"
    ]
    lines.append(
        "child_output: "
        f"stdout={'present' if completed.stdout else 'empty'} "
        f"stderr={'present' if completed.stderr else 'empty'} "
        f"stderr_lines={len((completed.stderr or '').splitlines())} "
        f"traceback={'yes' if 'Traceback' in (completed.stderr or '') else 'no'}"
    )
    # Django's test runner writes setup/migration diagnostics to stdout while
    # database-driver failures commonly use stderr. Inspect both streams, but
    # retain only the same bounded, sanitized markers from either one.
    for stream in (completed.stderr or "", completed.stdout or ""):
        for raw_line in stream.splitlines():
            candidate = _ANSI_ESCAPE_RE.sub("", raw_line.strip())
            database_errno_match = _DATABASE_ERRNO_RE.match(candidate)
            exception_match = _EXCEPTION_RE.fullmatch(candidate)
            test_failure_match = _TEST_FAILURE_RE.fullmatch(candidate)
            exception_kind = next(
                (
                    kind
                    for pattern, kind in _EXCEPTION_KIND_PATTERNS
                    if pattern.search(candidate)
                ),
                None,
            )
            if database_errno_match:
                lines.append(f"database_error: errno={database_errno_match.group(1)}")
            elif exception_kind:
                lines.append("exception:")
                lines.append(f"exception_kind: {exception_kind}")
            elif exception_match:
                lines.append("exception:")
            elif test_failure_match:
                lines.append(f"{test_failure_match.group(1)}: test_failed")
            elif _TEST_RESULT_RE.fullmatch(candidate):
                lines.append(candidate)
            elif _TEST_FAILURE_RESULT_RE.fullmatch(candidate):
                lines.append("FAILED (test_failed)")
    summary = "\n".join(lines) + "\n"
    return summary[:MAX_FAILURE_SUMMARY_CHARS]


class AdminClient:
    """Small DB-admin protocol backed by the pinned mysqlclient dependency."""

    def __init__(self, *, host: str, port: str, user: str, password: str):
        self.host = host
        self.port = str(port)
        self.user = user
        self.password = password

    def _connect(self):
        try:
            import MySQLdb
        except ImportError as exc:
            raise RuntimeError("mysqlclient is required for MariaDB admin operations") from exc
        return MySQLdb.connect(
            host=self.host,
            port=int(self.port),
            user=self.user,
            password=self.password,
            charset="utf8mb4",
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
            autocommit=True,
        )

    def _sql(self, statement: str) -> None:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(statement)
        finally:
            connection.close()

    def _query_one(self, statement: str):
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(statement)
                return cursor.fetchone()
        finally:
            connection.close()

    def _query_all(self, statement: str) -> list[tuple]:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(statement)
                return list(cursor.fetchall())
        finally:
            connection.close()

    def _show_create_table(self, database: str, table: str) -> str:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SHOW CREATE TABLE `{database}`.`{table}`")
                row = cursor.fetchone()
                return "" if not row else str(row[1])
        finally:
            connection.close()

    def server_identity(self) -> tuple[str, str]:
        version, version_comment = self._query_one(
            "SELECT VERSION(), @@version_comment"
        )
        return str(version), str(version_comment)

    def close(self) -> None:
        """Admin clients have no owned process; native servers override this."""

    def create_database(self, name: str) -> None:
        self._sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")

    def create_user(self, username: str, password: str) -> None:
        self._sql(f"CREATE USER '{username}'@'%' IDENTIFIED BY '{password}'")

    def grant_schema(self, username: str, database: str) -> None:
        self._sql(f"GRANT ALL PRIVILEGES ON `{database}`.* TO '{username}'@'%'")

    def ensure_namespace_absent(self, database: str, username: str) -> None:
        database_row = self._query_one(
            "SELECT COUNT(*) FROM information_schema.SCHEMATA "
            f"WHERE SCHEMA_NAME = '{database}'"
        )
        user_row = self._query_one(
            "SELECT COUNT(*) FROM mysql.user "
            f"WHERE User = '{username}'"
        )
        existing = []
        if database_row and database_row[0]:
            existing.append("database")
        if user_row and user_row[0]:
            existing.append("user")
        if existing:
            raise GateError(
                "Refusing MariaDB gate: generated "
                + " and ".join(existing)
                + " already exists"
            )

    def drop_user(self, username: str) -> None:
        # The gate creates only the `%` account.  Never delete a same-name
        # account created by another owner after the absence proof.
        self._sql(f"DROP USER IF EXISTS '{username}'@'%'")

    def drop_database(self, database: str) -> None:
        self._sql(f"DROP DATABASE IF EXISTS `{database}`")

    def verify_cleanup(self, database: str, username: str) -> tuple[bool, bool]:
        database_row = self._query_one(
            "SELECT COUNT(*) FROM information_schema.SCHEMATA "
            f"WHERE SCHEMA_NAME = '{database}'"
        )
        user_row = self._query_one(
            "SELECT COUNT(*) FROM mysql.user "
            f"WHERE User = '{username}'"
        )
        return bool(user_row[0]), bool(database_row[0])

    def verify_release_schema(self, database: str) -> dict[str, str]:
        if not re.fullmatch(r"test_twocomms_ig_[a-f0-9]{12}", database):
            raise GateError("MariaDB release schema name is not gate-owned")

        migration_row = self._query_one(
            f"SELECT COUNT(*) FROM `{database}`.`django_migrations` "
            "WHERE app = 'management' "
            f"AND name = '{_RELEASE_MIGRATION}'"
        )
        if not migration_row or int(migration_row[0]) != 1:
            raise GateError("MariaDB release migration is missing")

        column_rows = self._query_all(
            "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, "
            "CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = '{database}' "
            f"AND TABLE_NAME = '{_RELEASE_TABLE}' "
            "AND COLUMN_NAME IN "
            "('provider_message_id', 'delivery_provider_message_ids')"
        )
        columns = {
            str(name): (
                str(data_type).lower(),
                str(column_type).lower(),
                None if maximum_length is None else int(maximum_length),
            )
            for name, data_type, column_type, maximum_length in column_rows
        }
        if columns.get("provider_message_id") != ("varchar", "varchar(255)", 255):
            raise GateError("MariaDB provider receipt column does not match varchar(255)")

        delivery_column = columns.get("delivery_provider_message_ids")
        if not delivery_column or delivery_column[0] != "longtext":
            raise GateError("MariaDB delivery receipt column is not JSON-compatible")

        check_rows = self._query_all(
            "SELECT CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS "
            f"WHERE CONSTRAINT_SCHEMA = '{database}' "
            f"AND TABLE_NAME = '{_RELEASE_TABLE}'"
        )
        normalized_checks = [
            re.sub(r"\s+", "", str(row[0]).lower()).replace("`", "")
            for row in check_rows
        ]
        expected_json_check = "json_valid(delivery_provider_message_ids)"
        if expected_json_check not in normalized_checks:
            raise GateError("MariaDB delivery receipt JSON_VALID constraint is missing")

        return {
            "migration": f"management.{_RELEASE_MIGRATION}",
            "provider_message_id": "varchar(255)",
            "delivery_provider_message_ids": "longtext+json_valid",
        }

    def prepare_database_warning_contract(self, database: str) -> dict[str, str]:
        """Stage the warning migrations against production-like MyISAM tables."""
        if not re.fullmatch(r"test_twocomms_ig_[a-f0-9]{12}", database):
            raise GateError("MariaDB warning-contract schema name is not gate-owned")

        for app, migration in (
            ("storefront", "0097_mariadb_generated_uniqueness"),
            ("reviews", "0002_mariadb_vote_uniqueness"),
        ):
            row = self._query_one(
                f"SELECT COUNT(*) FROM `{database}`.`django_migrations` "
                f"WHERE app = '{app}' AND name = '{migration}'"
            )
            if not row or int(row[0]) != 0:
                raise GateError(
                    "MariaDB warning-contract target migration was applied too early"
                )

        tables = (
            "reviews_reviewvote",
            "storefront_productfitoption",
            "storefront_webpushdevicesubscription",
        )
        table_literals = ", ".join(f"'{table}'" for table in tables)
        existing_rows = self._query_all(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA = '{database}' "
            f"AND TABLE_NAME IN ({table_literals})"
        )
        if {str(row[0]) for row in existing_rows} != set(tables):
            raise GateError("MariaDB warning-contract staging table is missing")

        foreign_key_query = (
            "SELECT TABLE_NAME, CONSTRAINT_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE "
            f"WHERE CONSTRAINT_SCHEMA = '{database}' "
            "AND REFERENCED_TABLE_NAME IS NOT NULL "
            f"AND (TABLE_NAME IN ({table_literals}) "
            f"OR REFERENCED_TABLE_NAME IN ({table_literals}))"
        )
        foreign_key_rows = self._query_all(foreign_key_query)
        for raw_table, raw_constraint in foreign_key_rows:
            table = str(raw_table)
            constraint = str(raw_constraint)
            if not re.fullmatch(r"[A-Za-z0-9_]+", table) or not re.fullmatch(
                r"[A-Za-z0-9_]+",
                constraint,
            ):
                raise GateError("MariaDB warning-contract FK name is unsafe")
            self._sql(
                f"ALTER TABLE `{database}`.`{table}` "
                f"DROP FOREIGN KEY `{constraint}`"
            )
        if self._query_all(foreign_key_query):
            raise GateError("MariaDB warning-contract FK staging failed")

        for table in tables:
            self._sql(f"ALTER TABLE `{database}`.`{table}` ENGINE=MyISAM")

        engine_rows = self._query_all(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA = '{database}' "
            f"AND TABLE_NAME IN ({table_literals})"
        )
        engines = {str(table): str(engine).casefold() for table, engine in engine_rows}
        if engines != {table: "myisam" for table in tables}:
            raise GateError("MariaDB warning-contract MyISAM staging failed")
        return {"warning_contract_engines": "3_myisam"}

    def verify_database_warning_contract_reverse(self, database: str) -> dict[str, str]:
        """Prove reverse DDL removed only new objects and preserved endpoint."""
        if not re.fullmatch(r"test_twocomms_ig_[a-f0-9]{12}", database):
            raise GateError("MariaDB warning-contract schema name is not gate-owned")

        for app, migration in (
            ("storefront", "0097_mariadb_generated_uniqueness"),
            ("reviews", "0002_mariadb_vote_uniqueness"),
        ):
            row = self._query_one(
                f"SELECT COUNT(*) FROM `{database}`.`django_migrations` "
                f"WHERE app = '{app}' AND name = '{migration}'"
            )
            if not row or int(row[0]) != 0:
                raise GateError("MariaDB warning-contract reverse migration remains applied")

        tables = (
            "reviews_reviewvote",
            "storefront_productfitoption",
            "storefront_webpushdevicesubscription",
        )
        table_literals = ", ".join(f"'{table}'" for table in tables)
        engine_rows = self._query_all(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA = '{database}' "
            f"AND TABLE_NAME IN ({table_literals})"
        )
        engines = {str(table): str(engine).casefold() for table, engine in engine_rows}
        if engines != {table: "myisam" for table in tables}:
            raise GateError("MariaDB warning-contract reverse changed table engine")

        generated_rows = self._query_all(
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = '{database}' "
            "AND (TABLE_NAME, COLUMN_NAME) IN ("
            "('reviews_reviewvote', 'anon_identity'),"
            "('storefront_productfitoption', 'default_product_identity'),"
            "('storefront_webpushdevicesubscription', 'endpoint_digest'))"
        )
        if generated_rows:
            raise GateError("MariaDB warning-contract reverse left generated columns")

        unique_rows = self._query_all(
            "SELECT TABLE_NAME, INDEX_NAME, INDEX_TYPE, "
            "GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ',') "
            "FROM information_schema.STATISTICS "
            f"WHERE TABLE_SCHEMA = '{database}' AND NON_UNIQUE = 0 "
            f"AND TABLE_NAME IN ({table_literals}) "
            "GROUP BY TABLE_NAME, INDEX_NAME, INDEX_TYPE"
        )
        unique_indexes = {
            (
                str(table),
                str(name),
                str(index_type).casefold(),
                tuple(str(columns).split(",")),
            )
            for table, name, index_type, columns in unique_rows
        }
        if any(
            name in {
                "rev_vote_unique_user",
                "rev_vote_unique_anon",
                "uniq_default_fit_product",
                "uniq_webpush_endpoint_digest",
            }
            for _table, name, _index_type, _columns in unique_indexes
        ):
            raise GateError("MariaDB warning-contract reverse left new unique index")
        if (
            "storefront_webpushdevicesubscription",
            "endpoint",
            "hash",
            ("endpoint",),
        ) not in unique_indexes:
            raise GateError("MariaDB warning-contract reverse lost endpoint uniqueness")
        return {
            "warning_contract_reverse": "generated_removed+endpoint_hash_preserved"
        }

    def verify_database_warning_contract(self, database: str) -> dict[str, str]:
        """Prove the physical constraints that replaced former warnings."""
        if not re.fullmatch(r"test_twocomms_ig_[a-f0-9]{12}", database):
            raise GateError("MariaDB warning-contract schema name is not gate-owned")

        duplicate_queries = (
            "SELECT COUNT(*) FROM (SELECT 1 FROM "
            f"`{database}`.`reviews_reviewvote` WHERE `user_id` IS NOT NULL "
            "GROUP BY `review_id`, `user_id` HAVING COUNT(*) > 1) AS duplicates",
            "SELECT COUNT(*) FROM (SELECT 1 FROM "
            f"`{database}`.`reviews_reviewvote` WHERE `user_id` IS NULL "
            "AND `anon_key` <> '' GROUP BY `review_id`, `anon_key` "
            "HAVING COUNT(*) > 1) AS duplicates",
            "SELECT COUNT(*) FROM (SELECT 1 FROM "
            f"`{database}`.`storefront_productfitoption` WHERE `is_default` = 1 "
            "GROUP BY `product_id` HAVING COUNT(*) > 1) AS duplicates",
            "SELECT COUNT(*) FROM (SELECT 1 FROM "
            f"`{database}`.`storefront_webpushdevicesubscription` "
            "GROUP BY `endpoint` HAVING COUNT(*) > 1) AS duplicates",
        )
        for statement in duplicate_queries:
            row = self._query_one(statement)
            if not row or int(row[0]) != 0:
                raise GateError("MariaDB warning-contract duplicate scan failed")

        tables = (
            "reviews_reviewvote",
            "storefront_productfitoption",
            "storefront_webpushdevicesubscription",
        )
        table_literals = ", ".join(f"'{table}'" for table in tables)
        engine_rows = self._query_all(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA = '{database}' "
            f"AND TABLE_NAME IN ({table_literals})"
        )
        engines = {str(table): str(engine).casefold() for table, engine in engine_rows}
        if engines != {table: "myisam" for table in tables}:
            raise GateError("MariaDB warning-contract table is not MyISAM")

        unique_rows = self._query_all(
            "SELECT TABLE_NAME, INDEX_NAME, INDEX_TYPE, "
            "GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ',') "
            "FROM information_schema.STATISTICS "
            f"WHERE TABLE_SCHEMA = '{database}' AND NON_UNIQUE = 0 "
            f"AND TABLE_NAME IN ({table_literals}) "
            "GROUP BY TABLE_NAME, INDEX_NAME, INDEX_TYPE"
        )
        unique_indexes = {
            (
                str(table),
                str(name),
                str(index_type).casefold(),
                tuple(str(columns).split(",")),
            )
            for table, name, index_type, columns in unique_rows
        }
        forbidden_names = {
            "rev_vote_unique_per_user",
            "rev_vote_unique_per_anon",
            "uniq_default_fit_per_product",
        }
        if any(
            name in forbidden_names
            for _table, name, _index_type, _columns in unique_indexes
        ):
            raise GateError("MariaDB unsupported conditional constraint was created")
        forbidden_columns = {
            ("reviews_reviewvote", ("review_id", "anon_key")),
            ("storefront_productfitoption", ("product_id",)),
        }
        if any(
            (table, columns) in forbidden_columns
            for table, _name, _index_type, columns in unique_indexes
        ):
            raise GateError("MariaDB unsupported conditional constraint was created")

        required_indexes = {
            (
                "reviews_reviewvote",
                "rev_vote_unique_user",
                ("review_id", "user_id"),
            ),
            (
                "reviews_reviewvote",
                "rev_vote_unique_anon",
                ("review_id", "anon_identity"),
            ),
            (
                "storefront_productfitoption",
                "uniq_default_fit_product",
                ("default_product_identity",),
            ),
        }
        actual_named_indexes = {
            (table, name, columns)
            for table, name, _index_type, columns in unique_indexes
        }
        if not required_indexes.issubset(actual_named_indexes):
            raise GateError("MariaDB generated unique constraint is missing")
        if (
            "storefront_webpushdevicesubscription",
            "endpoint",
            "hash",
            ("endpoint",),
        ) not in unique_indexes:
            raise GateError("MariaDB endpoint HASH uniqueness was not preserved")

        required_columns = {
            ("storefront_productfitoption", ("product_id", "code")),
        }
        actual_columns = {
            (table, columns)
            for table, _name, _index_type, columns in unique_indexes
        }
        if not required_columns.issubset(actual_columns):
            raise GateError("MariaDB real unique constraint is missing")

        generated_rows = self._query_all(
            "SELECT TABLE_NAME, COLUMN_NAME, EXTRA, GENERATION_EXPRESSION "
            "FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = '{database}' "
            f"AND TABLE_NAME IN ({table_literals}) "
            "AND EXTRA LIKE '%GENERATED%'"
        )
        generated_columns = {
            (str(table), str(column)): (
                str(extra).lower(),
                re.sub(r"[\s`()]+", "", str(expression).lower()),
            )
            for table, column, extra, expression in generated_rows
        }
        expected_expressions = {
            ("reviews_reviewvote", "anon_identity"): (
                "stored generated",
                "casewhenuser_idisnullthenanon_keyelsenullend",
            ),
            ("storefront_productfitoption", "default_product_identity"): (
                "stored generated",
                "casewhenis_default=1thenproduct_idelsenullend",
            ),
        }
        if generated_columns != expected_expressions:
            raise GateError("MariaDB generated-column contract does not match")

        endpoint_row = self._query_one(
            "SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH "
            "FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = '{database}' "
            "AND TABLE_NAME = 'storefront_webpushdevicesubscription' "
            "AND COLUMN_NAME = 'endpoint'"
        )
        if (
            not endpoint_row
            or str(endpoint_row[0]).casefold() != "varchar"
            or int(endpoint_row[1] or 0) < 768
        ):
            raise GateError("MariaDB endpoint column capacity is below 768")

        create_sql = {table: self._show_create_table(database, table) for table in tables}
        normalized_create = {
            table: re.sub(r"\s+", "", sql.lower()).replace("`", "")
            for table, sql in create_sql.items()
        }
        if "rev_vote_user_or_anon_required" not in normalized_create["reviews_reviewvote"]:
            raise GateError("MariaDB review-vote check constraint is missing")
        if not all(normalized_create.values()):
            raise GateError("MariaDB SHOW CREATE TABLE proof is missing")
        if not all("engine=myisam" in sql for sql in normalized_create.values()):
            raise GateError("MariaDB SHOW CREATE TABLE engine proof is missing")
        for forbidden_name in forbidden_names:
            if any(forbidden_name in sql for sql in normalized_create.values()):
                raise GateError("MariaDB unsupported conditional constraint was created")
        if any("endpoint_digest" in sql for sql in normalized_create.values()):
            raise GateError("MariaDB endpoint digest must not exist")
        for required_name in {
            "rev_vote_unique_user",
            "rev_vote_unique_anon",
            "uniq_default_fit_product",
            "endpoint",
        }:
            if not any(required_name in sql for sql in normalized_create.values()):
                raise GateError("MariaDB required unique constraint is missing")

        return {
            "engines": "3_myisam",
            "generated_columns": "2_verified",
            "unique_indexes": "3_new+1_preserved",
            "endpoint_unique": "hash_preserved",
            "show_create_tables": "verified",
        }

    def verify_follow_ugc_schema(self, database: str) -> dict[str, str]:
        """Prove the latest feature migrations and MariaDB-only invariants."""
        if not re.fullmatch(r"test_twocomms_ig_[a-f0-9]{12}", database):
            raise GateError("MariaDB follow/UGC schema name is not gate-owned")

        for app, migration in (
            ("management", _FOLLOW_UGC_MIGRATION),
            ("storefront", _GUEST_PROMO_MIGRATION),
        ):
            row = self._query_one(
                f"SELECT COUNT(*) FROM `{database}`.`django_migrations` "
                f"WHERE app = '{app}' AND name = '{migration}'"
            )
            if not row or int(row[0]) != 1:
                raise GateError(f"MariaDB {app} follow/UGC migration is missing")

        table_literals = ", ".join(f"'{table}'" for table in _FOLLOW_UGC_TABLES)
        engine_rows = self._query_all(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA = '{database}' AND TABLE_NAME IN ({table_literals})"
        )
        engines = {str(table): str(engine).lower() for table, engine in engine_rows}
        missing_tables = sorted(set(_FOLLOW_UGC_TABLES) - set(engines))
        if missing_tables:
            raise GateError("MariaDB follow/UGC required table is missing")
        if any(engines[table] != "innodb" for table in _FOLLOW_UGC_TABLES):
            raise GateError("MariaDB follow/UGC table is not InnoDB")

        unique_rows = self._query_all(
            "SELECT TABLE_NAME, INDEX_NAME, "
            "GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ',') "
            "FROM information_schema.STATISTICS "
            f"WHERE TABLE_SCHEMA = '{database}' AND NON_UNIQUE = 0 "
            f"AND TABLE_NAME IN ({table_literals}) "
            "GROUP BY TABLE_NAME, INDEX_NAME"
        )
        actual_unique: dict[str, set[tuple[str, ...]]] = {}
        for table, _index, columns in unique_rows:
            actual_unique.setdefault(str(table), set()).add(
                tuple(str(columns).split(","))
            )
        for table, expected in _FOLLOW_UGC_UNIQUE_COLUMNS.items():
            if not expected.issubset(actual_unique.get(table, set())):
                raise GateError("MariaDB follow/UGC unique index is missing")

        foreign_key_rows = self._query_all(
            "SELECT TABLE_NAME, CONSTRAINT_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE "
            f"WHERE TABLE_SCHEMA = '{database}' "
            "AND REFERENCED_TABLE_NAME IS NOT NULL "
            f"AND TABLE_NAME IN ({table_literals})"
        )
        if foreign_key_rows:
            raise GateError("MariaDB follow/UGC ORM-only foreign-key policy was violated")

        lifecycle_column_rows = self._query_all(
            "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, "
            "CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE "
            "FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = '{database}' "
            "AND TABLE_NAME = 'management_igugcreward' "
            "AND COLUMN_NAME IN "
            "('lifecycle_state', 'lifecycle_reason', 'lifecycle_updated_at')"
        )
        lifecycle_columns = {
            str(name): (
                str(data_type).lower(),
                str(column_type).lower(),
                None if maximum_length is None else int(maximum_length),
                str(nullable).upper(),
            )
            for name, data_type, column_type, maximum_length, nullable
            in lifecycle_column_rows
        }
        if lifecycle_columns.get("lifecycle_state") != (
            "varchar", "varchar(16)", 16, "NO"
        ):
            raise GateError("MariaDB UGC lifecycle state column is invalid")
        if lifecycle_columns.get("lifecycle_reason") != (
            "varchar", "varchar(64)", 64, "NO"
        ):
            raise GateError("MariaDB UGC lifecycle reason column is invalid")
        updated_column = lifecycle_columns.get("lifecycle_updated_at")
        if not updated_column or updated_column[0] != "datetime" or updated_column[3] != "NO":
            raise GateError("MariaDB UGC lifecycle timestamp column is invalid")

        lifecycle_index_rows = self._query_all(
            "SELECT INDEX_NAME, "
            "GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ',') "
            "FROM information_schema.STATISTICS "
            f"WHERE TABLE_SCHEMA = '{database}' "
            "AND TABLE_NAME = 'management_igugcreward' "
            "GROUP BY INDEX_NAME"
        )
        lifecycle_index_columns = {
            tuple(str(columns).split(","))
            for _index, columns in lifecycle_index_rows
        }
        if not {
            ("lifecycle_state",),
            ("lifecycle_updated_at",),
        }.issubset(lifecycle_index_columns):
            raise GateError("MariaDB UGC lifecycle index is missing")

        lifecycle_job_index_rows = self._query_all(
            "SELECT INDEX_NAME, "
            "GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ',') "
            "FROM information_schema.STATISTICS "
            f"WHERE TABLE_SCHEMA = '{database}' "
            "AND TABLE_NAME = 'management_igugcrewardlifecyclejob' "
            "AND NON_UNIQUE = 1 "
            "GROUP BY INDEX_NAME"
        )
        lifecycle_job_index_columns = {
            tuple(str(columns).split(","))
            for _index, columns in lifecycle_job_index_rows
        }
        if not _FOLLOW_UGC_LIFECYCLE_JOB_INDEX_COLUMNS.issubset(
            lifecycle_job_index_columns
        ):
            raise GateError("MariaDB UGC lifecycle-job index is missing")

        lifecycle_job_check_rows = self._query_all(
            "SELECT CONSTRAINT_NAME, CHECK_CLAUSE "
            "FROM information_schema.CHECK_CONSTRAINTS "
            f"WHERE CONSTRAINT_SCHEMA = '{database}' "
            "AND TABLE_NAME = 'management_igugcrewardlifecyclejob' "
            "AND CONSTRAINT_NAME = 'ig_ugc_life_job_target'"
        )
        target_check_re = re.compile(
            r"(?:order_idisnotnullorclient_idisnotnull|"
            r"client_idisnotnullororder_idisnotnull)"
        )
        if not any(
            target_check_re.search(
                re.sub(r"[\s()]+", "", str(clause).lower()).replace(
                    chr(96), ""
                )
            )
            for constraint_name, clause in lifecycle_job_check_rows
            if str(constraint_name) == "ig_ugc_life_job_target"
        ):
            raise GateError("MariaDB UGC lifecycle-job target check is missing")

        return {
            "follow_ugc_migration": f"management.{_FOLLOW_UGC_MIGRATION}",
            "guest_promo_migration": f"storefront.{_GUEST_PROMO_MIGRATION}",
            "follow_ugc_tables": f"{len(_FOLLOW_UGC_TABLES)}_innodb",
            "follow_ugc_unique_indexes": "verified",
            "follow_ugc_foreign_keys": "orm_only",
            "follow_ugc_lifecycle": "3_columns+2_indexes",
            "follow_ugc_lifecycle_job": "target_check+5_indexes",
        }


def _generated_identifiers() -> tuple[str, str, str]:
    token = uuid.uuid4().hex[:12]
    return f"test_twocomms_ig_{token}", f"twc_ig_{token}", secrets.token_urlsafe(24)


def _canonical_host(value: str) -> str:
    host = (value or "").strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        return "loopback" if address.is_loopback else address.compressed
    return "loopback" if host in {"localhost", "localhost.localdomain"} else host


def _is_loopback(value: str) -> bool:
    return _canonical_host(value) == "loopback"


def _production_hosts(source: Mapping[str, str]) -> set[str]:
    return {
        _canonical_host(source.get(host_name) or "localhost")
        for database_name, host_name in (
            ("DB_NAME", "DB_HOST"),
            ("DB_NAME_DTF", "DB_HOST_DTF"),
        )
        if (source.get(database_name) or "").strip()
    }


def _validate_target_host(source: Mapping[str, str], host: str) -> None:
    canonical = _canonical_host(host)
    if canonical in _production_hosts(source):
        raise GateError("Refusing MariaDB gate: target matches a configured production database host")
    if not _is_loopback(host) and source.get("TEST_MARIADB_REMOTE_ALLOWED") != "1":
        raise GateError(
            "MariaDB gate target is remote; set TEST_MARIADB_REMOTE_ALLOWED=1 explicitly"
        )


def _validate_entrypoint(project_root: Path) -> Path:
    manage_path = Path(project_root) / "twocomms" / "manage.py"
    if not manage_path.is_file():
        raise GateError(f"Django entrypoint is missing: {manage_path}")
    return manage_path


def _validate_server_identity(identity: tuple[str, str]) -> tuple[str, str]:
    version, version_comment = (str(value) for value in identity)
    lowered = f"{version} {version_comment}".lower()
    if "mariadb" not in lowered or not version.startswith("11.4"):
        raise GateError(
            "MariaDB 11.4 is required; received "
            f"version={version!r} comment={version_comment!r}"
        )
    return version, version_comment


def _process_environment(source: Mapping[str, str]) -> dict[str, str]:
    environment = {
        key: value
        for key, value in source.items()
        if key in SAFE_ENV_NAMES
        and key not in PRODUCTION_ENV_NAMES
        and not any(key.startswith(prefix) for prefix in PROVIDER_ENV_PREFIXES)
    }
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _child_environment(
    source: Mapping[str, str], *, database: str, username: str, password: str,
    host: str, port: str, review_write_freeze_marker: Path,
) -> dict[str, str]:
    environment = _process_environment(source)
    environment.update({
        "SECRET_KEY": "test-secret-key-for-mariadb-gate",
        "TEST_MARIADB_NAME": database,
        "TEST_MARIADB_USER": username,
        "TEST_MARIADB_PASSWORD": password,
        "TEST_MARIADB_HOST": host,
        "TEST_MARIADB_PORT": str(port),
        "TEST_REVIEW_WRITE_FREEZE_MARKER": str(review_write_freeze_marker),
        "MANAGER_TG_BOT_TOKEN": "",
        "MANAGEMENT_TG_BOT_TOKEN": "",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
        "TELEGRAM_ADMIN_ID": "",
    })
    if not _is_loopback(host) and source.get("TEST_MARIADB_REMOTE_ALLOWED") == "1":
        environment["TEST_MARIADB_REMOTE_ALLOWED"] = "1"
    return environment


def _create_review_write_freeze_marker() -> tuple[tempfile.TemporaryDirectory, Path]:
    """Create a gate-owned marker required by the disposable canary migration."""

    directory = tempfile.TemporaryDirectory(prefix="twc-review-write-freeze-")
    marker = Path(directory.name) / "review_writes.frozen"
    try:
        marker.write_bytes(REVIEW_WRITE_FREEZE_MARKER_BYTES)
        marker.chmod(0o600)
        marker_stat = os.lstat(marker)
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or stat.S_IMODE(marker_stat.st_mode) != 0o600
            or marker_stat.st_uid != os.geteuid()
        ):
            raise GateError("gate-owned review write-freeze marker is invalid")
        return directory, marker
    except BaseException:
        directory.cleanup()
        raise


def _command_runner(args: list[str], **kwargs):
    return subprocess.run(args, **kwargs)


def _validate_suite(suite: str) -> tuple[str, ...]:
    try:
        return SUITES[suite]
    except KeyError as exc:
        raise GateError(f"unsupported MariaDB suite: {suite}") from exc


class NativeMariaDb:
    """Own a temporary loopback MariaDB instance for the native mode."""

    def __init__(
        self,
        *,
        binaries: Mapping[str, str],
        command_runner: Callable[..., subprocess.CompletedProcess],
        environment: Mapping[str, str] | None = None,
        project_root: Path = PROJECT_ROOT,
    ):
        self._binaries = binaries
        self._command_runner = command_runner
        self._environment = dict(environment or _process_environment(os.environ))
        self._project_root = project_root
        self._tempdir = None
        self._process = None
        self.admin = None

    def start(self) -> "NativeMariaDb":
        try:
            self._tempdir = tempfile.TemporaryDirectory(prefix="twc-ig-mariadb-")
            data_dir = Path(self._tempdir.name) / "data"
            socket_path = Path(self._tempdir.name) / "mariadb.sock"
            data_dir.mkdir()
            initialized = self._command_runner(
                [
                    self._binaries["mariadb-install-db"],
                    "--no-defaults",
                    f"--datadir={data_dir}",
                    "--auth-root-authentication-method=normal",
                    "--skip-test-db",
                ],
                cwd=str(self._project_root), env=self._environment,
                capture_output=True, text=True, check=False, timeout=60,
            )
            if initialized.returncode:
                raise GateError(f"native MariaDB initialization failed ({initialized.returncode})")
            port = _free_port()
            log_path = Path(self._tempdir.name) / "mariadb.err"
            self._process = subprocess.Popen(
                [
                    self._binaries["mariadbd"], "--no-defaults", f"--datadir={data_dir}",
                    "--bind-address=127.0.0.1", f"--port={port}",
                    f"--socket={socket_path}", f"--log-error={log_path}",
                    "--skip-name-resolve",
                ],
                cwd=str(self._project_root), env=self._environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.admin = AdminClient(host="127.0.0.1", port=str(port), user="root", password="")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise GateError("native MariaDB server exited during startup")
                try:
                    self.admin._sql("SELECT 1")
                    return self
                except Exception:
                    time.sleep(0.2)
            raise GateError("native MariaDB server did not become ready within 30 seconds")
        except BaseException as primary_error:
            try:
                self.close()
            except BaseException as cleanup_error:
                raise GateError(
                    "native MariaDB startup failed and cleanup failed",
                    primary_error=primary_error,
                    cleanup_errors=[cleanup_error],
                ) from primary_error
            raise

    def __getattr__(self, name):
        return getattr(self.admin, name)

    def close(self) -> None:
        try:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5)
        finally:
            if self._tempdir is not None:
                self._tempdir.cleanup()
                self._tempdir = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _native_admin(
    environ: Mapping[str, str],
    binaries: Mapping[str, str | None] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess] = _command_runner,
    project_root: Path = PROJECT_ROOT,
):
    supplied = binaries or {}
    required = {
        "mariadbd": supplied.get("mariadbd") or environ.get("MARIADB_SERVER_BIN"),
        "mariadb-install-db": supplied.get("mariadb-install-db") or environ.get("MARIADB_INSTALL_DB_BIN"),
    }
    if not required["mariadbd"] or not required["mariadb-install-db"]:
        import shutil
        path = environ.get("PATH")
        required["mariadbd"] = required["mariadbd"] or shutil.which("mariadbd", path=path)
        required["mariadb-install-db"] = required["mariadb-install-db"] or shutil.which(
            "mariadb-install-db", path=path
        )
    missing = [name for name, path in required.items() if not path]
    if missing:
        raise GateError("native MariaDB provisioning requires: " + ", ".join(missing))
    return NativeMariaDb(
        binaries=required,
        command_runner=command_runner,
        environment=_process_environment(environ),
        project_root=project_root,
    ).start()


def run_gate(
    *,
    server_mode: str,
    suite: str = DEFAULT_SUITE,
    admin: AdminClient | object | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess] = _command_runner,
    environ: Mapping[str, str] | None = None,
    project_root: Path = PROJECT_ROOT,
    output: TextIO | None = None,
    native_binaries: Mapping[str, str | None] | None = None,
) -> dict[str, str]:
    source = dict(os.environ if environ is None else environ)
    output = output or sys.stdout
    modules = _validate_suite(suite)
    if server_mode not in {"external", "native"}:
        raise GateError(f"unsupported MariaDB server mode: {server_mode}")
    manage_path = _validate_entrypoint(project_root)
    database, username, password = _generated_identifiers()
    host = (source.get("MARIADB_HOST") or source.get("TEST_MARIADB_HOST") or "127.0.0.1").strip()
    port = (source.get("MARIADB_PORT") or source.get("TEST_MARIADB_PORT") or "3306").strip()
    _validate_target_host(source, host)
    if admin is None:
        if server_mode == "native":
            admin = _native_admin(
                source,
                native_binaries,
                command_runner,
                project_root=project_root,
            )
        else:
            admin_user = (source.get("MARIADB_ADMIN_USER") or "root").strip()
            admin_password = source.get("MARIADB_ADMIN_PASSWORD") or ""
            if not admin_password:
                raise GateError("external MariaDB mode requires MARIADB_ADMIN_PASSWORD")
            admin = AdminClient(
                host=host,
                port=port,
                user=admin_user,
                password=admin_password,
            )
    host = getattr(admin, "host", host)
    port = str(getattr(admin, "port", port))
    _validate_target_host(source, host)
    primary_error = None
    result = None
    cleanup_errors: list[BaseException] = []
    database_attempted = False
    user_attempted = False
    version = version_comment = ""
    database_warning_count = 0
    marker_directory = None
    try:
        marker_directory, review_write_freeze_marker = _create_review_write_freeze_marker()
        try:
            version, version_comment = _validate_server_identity(admin.server_identity())
        except GateError:
            raise
        except BaseException as exc:
            raise GateError("MariaDB server identity query failed") from exc
        ensure_namespace_absent = getattr(admin, "ensure_namespace_absent", None)
        if ensure_namespace_absent is None:
            raise GateError("MariaDB admin client cannot prove generated namespace ownership")
        ensure_namespace_absent(database, username)
        # The absence proof establishes ownership before CREATE so an ambiguous
        # post-CREATE transport failure is still cleaned up safely.
        database_attempted = True
        user_attempted = True
        admin.create_database(database)
        admin.create_user(username, password)
        admin.grant_schema(username, database)
        child_env = _child_environment(
            source, database=database, username=username, password=password,
            host=host, port=port,
            review_write_freeze_marker=review_write_freeze_marker,
        )
        for app, migration in (
            ("storefront", "0096"),
            ("reviews", "0001"),
        ):
            pre_migrate_command = [
                sys.executable,
                str(manage_path),
                "migrate",
                app,
                migration,
                "--settings=test_settings_mariadb",
                "--noinput",
            ]
            pre_migrate_completed = command_runner(
                pre_migrate_command,
                cwd=str(project_root / "twocomms"),
                env=child_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if pre_migrate_completed.returncode:
                output.write(
                    _failure_summary(
                        suite=f"pre-migrate-{app}",
                        completed=pre_migrate_completed,
                    )
                )
                primary_error = RuntimeError(
                    f"pre-migrate {app} command failed "
                    f"({pre_migrate_completed.returncode})"
                )
                raise primary_error

        prepare_warning_contract = getattr(
            admin,
            "prepare_database_warning_contract",
            None,
        )
        if prepare_warning_contract is None:
            raise GateError("MariaDB admin client cannot stage warning contract")
        preparation_evidence = prepare_warning_contract(database)
        output.write(
            "MariaDB warning contract staging: "
            f"engines={preparation_evidence['warning_contract_engines']}\n"
        )

        command = [
            sys.executable,
            str(manage_path),
            "test",
            *modules,
            "--settings=test_settings_mariadb",
            "--noinput",
            "--keepdb",
        ]
        completed = command_runner(
            command,
            cwd=str(project_root / "twocomms"),
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            output.write(_failure_summary(suite=suite, completed=completed))
            primary_error = RuntimeError(f"{suite} command failed ({completed.returncode})")
            raise primary_error

        for app, migration in (
            ("reviews", "0001"),
            ("storefront", "0096"),
        ):
            reverse_command = [
                sys.executable,
                str(manage_path),
                "migrate",
                app,
                migration,
                "--settings=test_settings_mariadb",
                "--noinput",
            ]
            reverse_completed = command_runner(
                reverse_command,
                cwd=str(project_root / "twocomms"),
                env=child_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if reverse_completed.returncode:
                output.write(
                    _failure_summary(
                        suite=f"reverse-{app}",
                        completed=reverse_completed,
                    )
                )
                primary_error = RuntimeError(
                    f"reverse {app} command failed ({reverse_completed.returncode})"
                )
                raise primary_error

        verify_warning_reverse = getattr(
            admin,
            "verify_database_warning_contract_reverse",
            None,
        )
        if verify_warning_reverse is None:
            raise GateError("MariaDB admin client cannot verify warning reverse")
        reverse_evidence = verify_warning_reverse(database)
        output.write(
            "MariaDB warning contract reverse: "
            f"status={reverse_evidence['warning_contract_reverse']}\n"
        )

        reapply_command = [
            sys.executable,
            str(manage_path),
            "migrate",
            "--settings=test_settings_mariadb",
            "--noinput",
        ]
        reapply_completed = command_runner(
            reapply_command,
            cwd=str(project_root / "twocomms"),
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if reapply_completed.returncode:
            output.write(
                _failure_summary(
                    suite="reapply-warning-contract",
                    completed=reapply_completed,
                )
            )
            primary_error = RuntimeError(
                "warning-contract reapply command failed "
                f"({reapply_completed.returncode})"
            )
            raise primary_error

        check_command = [
            sys.executable,
            str(manage_path),
            "check",
            "--settings=test_settings_mariadb",
            "--database=default",
            "--fail-level=WARNING",
        ]
        check_completed = command_runner(
            check_command,
            cwd=str(project_root / "twocomms"),
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if check_completed.returncode:
            output.write(
                _failure_summary(
                    suite="database-check",
                    completed=check_completed,
                )
            )
            primary_error = RuntimeError(
                f"database check command failed ({check_completed.returncode})"
            )
            raise primary_error
        warning_policy = classify_database_check_warnings(
            f"{check_completed.stdout}\n{check_completed.stderr}"
        )
        blocked_warnings = warning_policy["blocked"]
        missing_warnings = warning_policy["missing"]
        if blocked_warnings or missing_warnings:
            output.write(
                "MariaDB database check blocked warnings: "
                + ",".join(blocked_warnings or missing_warnings)
                + "\n"
            )
            primary_error = RuntimeError("database check warning policy failed")
            raise primary_error
        database_warning_count = int(warning_policy["allowed_count"])
        output.write(
            "MariaDB database check: alias=default status=passed "
            f"allowed_warnings={database_warning_count}\n"
        )
        verify_release_schema = getattr(admin, "verify_release_schema", None)
        if verify_release_schema is None:
            raise GateError("MariaDB admin client cannot verify release schema")
        schema_evidence = verify_release_schema(database)
        output.write(
            "MariaDB schema proof: "
            "migration=management.0156_ig_order_event_delivery_receipts "
            "provider_message_id=varchar(255) "
            "delivery_provider_message_ids=longtext+json_valid\n"
        )
        verify_warning_contract = getattr(
            admin, "verify_database_warning_contract", None
        )
        if verify_warning_contract is None:
            raise GateError("MariaDB admin client cannot verify warning contract")
        warning_evidence = verify_warning_contract(database)
        output.write(
            "MariaDB warning contract proof: "
            f"engines={warning_evidence['engines']} "
            f"generated_columns={warning_evidence['generated_columns']} "
            f"unique_indexes={warning_evidence['unique_indexes']} "
            f"endpoint_unique={warning_evidence['endpoint_unique']} "
            f"show_create_tables={warning_evidence['show_create_tables']}\n"
        )
        feature_evidence = {}
        if suite == "follow-ugc-concurrency":
            verify_follow_ugc_schema = getattr(admin, "verify_follow_ugc_schema", None)
            if verify_follow_ugc_schema is None:
                raise GateError("MariaDB admin client cannot verify follow/UGC schema")
            feature_evidence = verify_follow_ugc_schema(database)
            output.write(
                "MariaDB follow/UGC schema proof: "
                f"migration={feature_evidence['follow_ugc_migration']} "
                f"guest_promo={feature_evidence['guest_promo_migration']} "
                f"tables={feature_evidence['follow_ugc_tables']} "
                f"unique_indexes={feature_evidence['follow_ugc_unique_indexes']} "
                f"foreign_keys={feature_evidence['follow_ugc_foreign_keys']} "
                f"lifecycle={feature_evidence['follow_ugc_lifecycle']} "
                f"lifecycle_job={feature_evidence['follow_ugc_lifecycle_job']}\n"
            )
        result = {
            "status": "passed",
            "database": database,
            "suite": suite,
            "version": version,
            "version_comment": version_comment,
            "database_check": (
                f"default:passed;allowed_warnings={database_warning_count}"
            ),
            **preparation_evidence,
            **reverse_evidence,
            **schema_evidence,
            **warning_evidence,
            **feature_evidence,
        }
    except BaseException as exc:
        primary_error = primary_error or exc
    finally:
        if user_attempted:
            try:
                admin.drop_user(username)
            except BaseException as exc:
                cleanup_errors.append(exc)
        if database_attempted:
            try:
                admin.drop_database(database)
            except BaseException as exc:
                cleanup_errors.append(exc)
        if (user_attempted or database_attempted) and hasattr(admin, "verify_cleanup"):
            try:
                user_exists, database_exists = admin.verify_cleanup(database, username)
                if user_exists or database_exists:
                    cleanup_errors.append(
                        RuntimeError(
                            "cleanup verification found residue: "
                            f"user={bool(user_exists)} database={bool(database_exists)}"
                        )
                    )
            except BaseException as exc:
                cleanup_errors.append(exc)
        close = getattr(admin, "close", None)
        if close:
            try:
                close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if marker_directory is not None:
            try:
                marker_directory.cleanup()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            if primary_error:
                raise GateError(
                    "MariaDB gate failed and cleanup failed",
                    primary_error=primary_error,
                    cleanup_errors=cleanup_errors,
                ) from primary_error
            raise GateError(
                "MariaDB gate cleanup failed", cleanup_errors=cleanup_errors
            ) from cleanup_errors[0]
    if primary_error:
        if isinstance(primary_error, GateError):
            raise primary_error
        raise GateError("MariaDB gate failed", primary_error=primary_error) from primary_error
    result["cleanup"] = "verified"
    output.write(
        f"MariaDB gate passed: mode={server_mode} suite={suite} "
        f"version={version} database={database} cleanup=verified\n"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-mode", choices=("native", "external"), required=True)
    parser.add_argument("--suite", choices=tuple(SUITES), default=DEFAULT_SUITE)
    args = parser.parse_args(argv)
    try:
        run_gate(server_mode=args.server_mode, suite=args.suite)
    except GateError as exc:
        print(f"MariaDB gate failed: {exc}", file=sys.stderr)
        return 1
    except BaseException:
        print("MariaDB gate failed: unexpected_error", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
