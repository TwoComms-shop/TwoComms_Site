import os

"""
Django Test Settings для запуска тестов с SQLite вместо MySQL.

Использование:
    python manage.py test --settings=test_settings
    coverage run --source=storefront manage.py test --settings=test_settings
"""

# Устанавливаем тестовый SECRET_KEY перед импортом settings.
os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-testing-only-do-not-use-in-production')

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
):
    os.environ[_telegram_env_name] = ''

from twocomms.settings import *  # noqa: F401,F403


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
]
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_DOMAIN = None
CSRF_COOKIE_DOMAIN = None
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
NOVA_POSHTA_FALLBACK_ENABLED = False
TESTING = True
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
