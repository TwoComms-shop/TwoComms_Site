"""
Локальные настройки для визуальной проверки в песочнице (runserver).
НЕ для продакшена. Файловая SQLite вместо :memory:, компрессия выключена.
"""
from test_settings import *  # noqa: F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': '/tmp/twc_preview.sqlite3',
    }
}

DEBUG = True
ALLOWED_HOSTS = ['*']
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
COMPRESS_ENABLED = False
COMPRESS_OFFLINE = False
