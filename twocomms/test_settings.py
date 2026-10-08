import os
import tempfile
from pathlib import Path

"""
Django Test Settings для запуска тестов с SQLite вместо MySQL.

Использование:
    python manage.py test --settings=test_settings
    coverage run --source=storefront manage.py test --settings=test_settings
"""

# Never let a production or developer secret leak into the deterministic test
# settings profile when manage.py has loaded a local environment file first.
os.environ['SECRET_KEY'] = 'test-secret-key-for-testing-only-do-not-use-in-production'
# Keep the imported base settings from probing an offline compressor manifest.
# The deterministic profile sets DEBUG=False again after the import below.
os.environ['DEBUG'] = 'True'

# Test runs are also executed on the production host. The notification layer
# reads these values directly from ``os.environ`` (not only from Django
# settings), so inherited production credentials would make tests contact the
# real Telegram API. Empty them before importing the base settings; individual
# tests that exercise delivery pass explicit fake credentials or patch env.
for _telegram_env_name in (
    'TELEGRAM_BOT_TOKEN',
    'TELEGRAM_CHAT_ID',
    'TELEGRAM_ADMIN_ID',
    'TELEGRAM_STORAGE_BOT_TOKEN',
    'TELEGRAM_STORAGE_CHAT_IDS',
    'MANAGER_TG_BOT_TOKEN',
    'MANAGEMENT_TG_BOT_TOKEN',
):
    os.environ[_telegram_env_name] = ''

from twocomms.settings import *  # noqa: F401,F403

# Test runs never activate a background provider from production defaults.
# V2 publication/admission suites explicitly enable the exact gates they test;
# legacy compatibility fixtures deliberately retain their v1 contract.
IG_MEMORY_TIMELINE_ENABLED = False
IG_MEMORY_GENERATION_ENABLED = False
IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED = False

# Test-only Fernet key: custom bot credentials must exercise encrypted storage
# instead of silently falling back to plaintext in SQLite tests.
FIELD_ENCRYPTION_KEY = 'Tj-k7EnSDEgaPpRWR9lEGgp2DmQ4LgU6L6-3P5qiv5U='

# Dedicated deterministic keyring for UGC lifetime-identity tests.  Production
# must supply its own retained keyring through environment variables.
IG_UGC_IDENTITY_HMAC_ACTIVE_KEY_ID = 'test-v1'
IG_UGC_IDENTITY_HMAC_KEYRING = {
    'test-v1': 'test-ugc-identity-hmac-key-0000000000000001',
}


# Используем SQLite для тестов (быстрее и не требует MySQL)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',  # Хранить БД в памяти для скорости
    }
}

# Отключаем миграции для ускорения тестов (не обязательно)


class DisableMigrations:
    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


MIGRATION_MODULES = DisableMigrations()

# Детерминированный профиль тестового окружения.
DEBUG = False
IG_PRIVATE_MEDIA_ROOT = str(Path(
    tempfile.mkdtemp(prefix="twocomms-test-private-media-")
).resolve())
os.chmod(IG_PRIVATE_MEDIA_ROOT, 0o700)
SECURE_SSL_REDIRECT = False
ALLOWED_HOSTS = [
    'testserver',
    'test.com',
    'localhost',
    '127.0.0.1',
    'twocomms.shop',
    'www.twocomms.shop',
    'dtf.twocomms.shop',
    'management.twocomms.shop',
    'storage.twocomms.shop',
]
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_DOMAIN = None
CSRF_COOKIE_DOMAIN = None
MAILERS = {
    alias: {
        'BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    }
    for alias in ('default', 'transactional', 'reports')
}
EMAIL_REPLY_TO_ADDRESS = 'test@example.invalid'
EMAIL_DELIVERY_CONFIGURED = False
NOVA_POSHTA_FALLBACK_ENABLED = False
TESTING = True
IG_ANALYSIS_V2_MODE = 'off'
IG_ANALYSIS_V2_EXTENDED_PROMPT = False
IG_TYPED_MEMORY_MODE = 'off'
IG_TYPED_MEMORY_HMAC_ACTIVE_KEY_ID = 'tmk_test_v1'
IG_TYPED_MEMORY_HMAC_KEYRING = {
    'tmk_test_v1': 'test-typed-memory-hmac-key-00000000000001',
}
IG_ASSISTED_CHECKOUT_V2 = 'off'
IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT = 0
# Post-commit production wake-ups must not start detached workers against the
# in-memory test database. Their durable reconciliation is tested explicitly.
IG_FULFILLMENT_BACKGROUND_WAKE_ENABLED = False
SIMPLE_RATE_LIMIT_ENABLED = False
COMPRESS_ENABLED = False
COMPRESS_OFFLINE = False

# Изоляция Celery: broker/result backend не должны ходить в Redis/RabbitMQ.
CELERY_BROKER_URL = 'memory://'
CELERY_RESULT_BACKEND = 'cache+memory://'
CELERY_TASK_ALWAYS_EAGER = False
CELERY_TASK_IGNORE_RESULT = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = False

# Простой пароль хэшер для ускорения тестов
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

# Отключаем кэширование в тестах
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'twocomms-test-cache',
    },
    'fragments': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'twocomms-test-fragments-cache',
    },
}

STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
}

# Минимальное логирование
LOGGING = {
    'version': 1,
    'disable_existing_loggers': True,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'ERROR',
    },
}

# Отключаем CSRF для тестов
CSRF_USE_SESSIONS = False
