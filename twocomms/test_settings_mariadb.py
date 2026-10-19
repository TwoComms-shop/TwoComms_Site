"""Fail-closed settings for disposable MariaDB test runs.

This profile intentionally never reads ``DB_*`` to construct its connection.
Set a dedicated ``TEST_MARIADB_*`` credential set for a disposable MariaDB
instance, for example::

    TEST_MARIADB_NAME=test_twocomms_ig_gate \\
    TEST_MARIADB_USER=... \\
    TEST_MARIADB_PASSWORD=... \\
    python manage.py test --settings=test_settings_mariadb ...
"""

import os
import re
import stat
from ipaddress import ip_address
from pathlib import Path

from test_network_guard import install_external_network_guard


install_external_network_guard()


_TEST_DATABASE_NAME_RE = re.compile(r"test_twocomms_[A-Za-z0-9_]+$")
_LOOPBACK_HOSTS = {"localhost", "::1"}
_REVIEW_WRITE_FREEZE_MARKER_BYTES = b"review-write-freeze-v1\n"


def _required_environment(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(
            f"{name} must be set for the disposable MariaDB test profile."
        )
    return value


def _canonical_host(value: str) -> str:
    host = (value or "").strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if address.is_loopback:
            return "loopback"
        return address.compressed
    if host in _LOOPBACK_HOSTS:
        return "loopback"
    return host


def _review_write_freeze_marker() -> str:
    value = (os.environ.get("TEST_REVIEW_WRITE_FREEZE_MARKER") or "").strip()
    if not value:
        raise RuntimeError(
            "TEST_REVIEW_WRITE_FREEZE_MARKER must be set for the disposable "
            "MariaDB test profile."
        )
    path = Path(value)
    try:
        path_stat = os.lstat(path)
    except OSError as exc:
        raise RuntimeError("disposable MariaDB review write-freeze marker is invalid") from exc
    if (
        not path.is_absolute()
        or stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
        or stat.S_IMODE(path_stat.st_mode) != 0o600
        or path_stat.st_uid != os.geteuid()
    ):
        raise RuntimeError("disposable MariaDB review write-freeze marker is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or stat.S_IMODE(opened_stat.st_mode) != 0o600
                or opened_stat.st_uid != os.geteuid()
                or (opened_stat.st_dev, opened_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)
                or os.read(descriptor, len(_REVIEW_WRITE_FREEZE_MARKER_BYTES) + 1)
                != _REVIEW_WRITE_FREEZE_MARKER_BYTES
            ):
                raise RuntimeError("disposable MariaDB review write-freeze marker is invalid")
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise RuntimeError("disposable MariaDB review write-freeze marker is invalid") from exc
    return str(path)


def _test_database_configuration() -> tuple[str, str, str, str, str]:
    name = _required_environment("TEST_MARIADB_NAME")
    user = _required_environment("TEST_MARIADB_USER")
    password = _required_environment("TEST_MARIADB_PASSWORD")
    host = (os.environ.get("TEST_MARIADB_HOST") or "127.0.0.1").strip()
    raw_port = (os.environ.get("TEST_MARIADB_PORT") or "3306").strip()

    if not _TEST_DATABASE_NAME_RE.fullmatch(name) or len(name) > 64:
        raise RuntimeError(
            "TEST_MARIADB_NAME must match test_twocomms_<suffix> and be at most "
            "64 characters."
        )
    try:
        port_number = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "TEST_MARIADB_PORT must be an integer from 1 to 65535."
        ) from exc
    if not 1 <= port_number <= 65535:
        raise RuntimeError("TEST_MARIADB_PORT must be an integer from 1 to 65535.")

    if test_host := _canonical_host(host):
        if test_host != "loopback" and os.environ.get("TEST_MARIADB_REMOTE_ALLOWED") != "1":
            raise RuntimeError(
                "TEST_MARIADB_HOST is remote; set TEST_MARIADB_REMOTE_ALLOWED=1 "
                "explicitly for a disposable MariaDB service."
            )

    production_names = {
        value.strip().lower()
        for value in (os.environ.get("DB_NAME"), os.environ.get("DB_NAME_DTF"))
        if (value or "").strip()
    }
    if name.lower() in production_names:
        raise RuntimeError(
            "Refusing MariaDB tests: TEST_MARIADB_NAME matches a configured "
            "production database."
        )

    production_users = {
        value.strip().lower()
        for value in (os.environ.get("DB_USER"), os.environ.get("DB_USER_DTF"))
        if (value or "").strip()
    }
    if user.lower() in production_users:
        raise RuntimeError(
            "Refusing MariaDB tests: TEST_MARIADB_USER matches a configured "
            "production database user."
        )

    test_host = _canonical_host(host)
    # Django treats an omitted MySQL HOST as ``localhost``. Account for that
    # effective value only when the matching configured database exists, so a
    # disposable loopback instance cannot silently point at production.
    production_hosts = {
        _canonical_host((host or "").strip() or "localhost")
        for database_name, host in (
            (os.environ.get("DB_NAME"), os.environ.get("DB_HOST")),
            (os.environ.get("DB_NAME_DTF"), os.environ.get("DB_HOST_DTF")),
        )
        if (database_name or "").strip()
    }
    if test_host and test_host in production_hosts:
        raise RuntimeError(
            "Refusing MariaDB tests: TEST_MARIADB_HOST matches a configured "
            "production database host."
        )

    return name, user, password, host, str(port_number)


_NAME, _USER, _PASSWORD, _HOST, _PORT = _test_database_configuration()
_REVIEW_WRITE_FREEZE_MARKER = _review_write_freeze_marker()

# Keep all no-network test isolation from the normal SQLite profile. This must
# follow validation: importing the settings with incomplete test credentials is
# an error rather than a fallback to SQLite or the application's DB_* settings.
from test_settings import *  # noqa: E402,F401,F403


INSTALLED_APPS = [
    app
    for app in INSTALLED_APPS
    if app not in {"dtf", "dtf.apps.DtfConfig"}
]
INSTALLED_APPS.append("test_support.dtf_stub.apps.DtfStubConfig")
ALLOWED_HOSTS = [host for host in ALLOWED_HOSTS if "dtf" not in host.casefold()]


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": _NAME,
        "USER": _USER,
        "PASSWORD": _PASSWORD,
        "HOST": _HOST,
        "PORT": _PORT,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": True,
        "TEST": {
            "NAME": _NAME,
            "MIGRATE": True,
            "CHARSET": "utf8mb4",
        },
        "OPTIONS": {
            "charset": "utf8mb4",
            "collation": "utf8mb4_unicode_ci",
            "use_unicode": True,
            "init_command": "SET SESSION default_storage_engine=INNODB",
            "sql_mode": (
                "STRICT_TRANS_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_ZERO_DATE,"
                "NO_ZERO_IN_DATE,NO_ENGINE_SUBSTITUTION"
            ),
        },
    }
}

# The SQLite profile disables migrations. A MariaDB proof must instead exercise
# the actual migration graph and has no reason to retain the optional DTF alias.
MIGRATION_MODULES = {
    "dtf": "test_support.dtf_stub.migrations",
    "warehouse": "test_support.warehouse_migrations_non_dtf",
}
DATABASE_ROUTERS = []
TEST_NETWORK_POLICY = "deny-external-allow-loopback"
TEST_DTF_SCOPE = "excluded-with-dependency-stub"
REVIEW_WRITE_FREEZE_MARKER = _REVIEW_WRITE_FREEZE_MARKER
TEST_RUNNER = "test_support.ig_mariadb_runner.InstagramMariaDbGateRunner"
