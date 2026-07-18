"""
Эффективные настройки кеширования для TwoComms
"""

import os
import re
from pathlib import Path
from functools import lru_cache


IMMUTABLE_ASSET_RE = re.compile(r"^.+\.[0-9a-f]{8,}\..+$")


@lru_cache(maxsize=4096)
def _cached_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def is_immutable_static_url(path, url):
    """
    WhiteNoise callback: immutable only for content-hashed asset URLs.
    """
    if not url:
        return False
    return bool(IMMUTABLE_ASSET_RE.match(str(url)))


def add_cache_headers(headers, path, url):
    """
    Добавляет эффективные заголовки кеширования для разных типов ресурсов
    """
    # Специальные заголовки для Service Worker
    if path.endswith('sw.js') or 'sw.js' in path:
        service_worker_csp = (
            "default-src 'self'; "
            "connect-src 'self' "
            "https://www.googletagmanager.com https://googletagmanager.com https://tagmanager.google.com "
            "https://www.google-analytics.com https://ssl.google-analytics.com https://analytics.google.com "
            "https://region1.analytics.google.com https://region1.google-analytics.com "
            "https://www.googleadservices.com https://googleads.g.doubleclick.net https://*.doubleclick.net "
            "https://www.google.com https://*.google.com "
            "https://www.facebook.com https://connect.facebook.net https://graph.facebook.com https://*.facebook.com "
            "https://analytics.tiktok.com https://ads.tiktok.com "
            "https://www.clarity.ms https://scripts.clarity.ms https://*.clarity.ms; "
            "script-src 'self'; "
            "worker-src 'self'; "
        )
        headers['Cache-Control'] = 'public, max-age=0, must-revalidate'
        headers['Content-Security-Policy'] = service_worker_csp
        headers['Service-Worker-Allowed'] = '/'
        headers['Vary'] = 'Accept-Encoding'
        headers['X-Content-Type-Options'] = 'nosniff'
        headers['X-Frame-Options'] = 'DENY'
        return headers

    # Определяем тип файла по расширению
    file_extension = Path(path).suffix.lower()
    is_immutable = is_immutable_static_url(path, url)

    if is_immutable:
        headers['Cache-Control'] = 'public, max-age=31536000, immutable'

    # CSS/JS without content hash should revalidate sooner to avoid sticky stale assets.
    elif file_extension in ['.css', '.js']:
        headers['Cache-Control'] = 'public, max-age=86400'
        headers['ETag'] = f'"{_cached_mtime(path)}"'

    elif file_extension in ['.webmanifest']:
        headers['Cache-Control'] = 'public, max-age=0, must-revalidate'
        headers['ETag'] = f'"{_cached_mtime(path)}"'

    # Phase 22f (2026-05-13) — изображения без content-hash в URL.
    # Lighthouse mobile «эффективный период хранения кеша» flag'ает 7d TTL
    # как слишком короткий. Поднимаем до 30 дней + ETag для revalidate:
    # браузер быстро проверит ETag (304 Not Modified) если файл изменился,
    # но в большинстве случаев берёт из cache без сетевого запроса вообще.
    # Product images, category icons, brand logos — почти не меняются.
    elif file_extension in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.svg', '.ico']:
        headers['Cache-Control'] = 'public, max-age=2592000'  # 30 дней
        headers['ETag'] = f'"{_cached_mtime(path)}"'

    # Phase 22f (2026-05-13) — шрифты теперь immutable-1y. Inter.woff2
    # versioned (хеш в имени файла) или фактически неизменяемые — riski
    # «stale font» практически нулевой, бенефит от 1-year TTL огромный
    # (нет вообще запроса на повторных визитах).
    elif file_extension in ['.woff', '.woff2', '.ttf', '.eot']:
        headers['Cache-Control'] = 'public, max-age=31536000, immutable'

    # HTML файлы - кешируем на 1 час
    elif file_extension in ['.html', '.htm']:
        headers['Cache-Control'] = 'public, max-age=3600'  # 1 час
        headers['ETag'] = f'"{_cached_mtime(path)}"'

    # Медиа файлы - кешируем на 30 дней
    elif file_extension in ['.mp4', '.mp3', '.avi', '.mov']:
        headers['Cache-Control'] = 'public, max-age=2592000'  # 30 дней
        headers['ETag'] = f'"{_cached_mtime(path)}"'

    # Остальные файлы - кешируем на 7 дней
    else:
        headers['Cache-Control'] = 'public, max-age=604800'  # 7 дней
        headers['ETag'] = f'"{_cached_mtime(path)}"'

    # Добавляем заголовки для оптимизации
    headers['Vary'] = 'Accept-Encoding'
    headers['X-Content-Type-Options'] = 'nosniff'
    headers['X-Frame-Options'] = 'DENY'

    return headers


def get_media_cache_headers(path):
    """
    Возвращает заголовки кеширования для медиа файлов
    """
    headers = {}
    file_extension = Path(path).suffix.lower()

    # Аватары и прочие пользовательские изображения должны обновляться заметно быстрее.
    if 'avatars' in path:
        headers['Cache-Control'] = 'public, max-age=86400, must-revalidate'  # 1 день
        headers['ETag'] = f'"{_cached_mtime(path)}"'

    # Изображения/медиа upload-папки mutable по URL, поэтому без hash-name не даём годовой TTL.
    # Phase 22f (2026-05-13) — но 1d TTL слишком короткий: Lighthouse flag'ает
    # product images как «эффективный период хранения кеша – 50 KiB». Поднимаем
    # до 30 дней + ETag — браузер revalidate'ит через 304 если файл изменился.
    elif file_extension in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.svg', '.ico']:
        if IMMUTABLE_ASSET_RE.match(Path(path).name):
            headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        else:
            headers['Cache-Control'] = 'public, max-age=2592000'  # 30 дней
        headers['ETag'] = f'"{_cached_mtime(path)}"'

    # Видео/аудио тоже считаем mutable, если URL не versioned.
    elif file_extension in ['.mp4', '.mp3', '.avi', '.mov', '.webm']:
        headers['Cache-Control'] = 'public, max-age=86400, must-revalidate'  # 1 день
        headers['ETag'] = f'"{_cached_mtime(path)}"'

    # Остальные медиа файлы - кешируем на 1 день
    else:
        headers['Cache-Control'] = 'public, max-age=86400, must-revalidate'  # 1 день
        headers['ETag'] = f'"{_cached_mtime(path)}"'

    return headers
