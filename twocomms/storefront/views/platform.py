from pathlib import Path

from django.contrib.staticfiles import finders
from django.http import FileResponse, Http404, HttpResponse

from twocomms.cache_headers import add_cache_headers


def _resolve_static_asset(relative_path):
    asset_path = finders.find(relative_path)
    if isinstance(asset_path, (list, tuple)):
        asset_path = asset_path[0] if asset_path else None
    if not asset_path:
        raise Http404(f"{relative_path} not found")
    return Path(asset_path)


def _serve_static_text_asset(request, relative_path, content_type):
    file_path = _resolve_static_asset(relative_path)
    content = file_path.read_text(encoding="utf-8")
    response = HttpResponse(content, content_type=content_type)
    add_cache_headers(response, str(file_path), request.path)
    return response


def service_worker_script(request):
    response = _serve_static_text_asset(
        request,
        "sw.js",
        "application/javascript; charset=utf-8",
    )
    response["Service-Worker-Allowed"] = "/"
    return response


def web_manifest(request):
    return _serve_static_text_asset(
        request,
        "site.webmanifest",
        "application/manifest+json; charset=utf-8",
    )


def favicon_ico(request):
    """Serve the root favicon directly so crawlers do not pay a redirect hop."""
    file_path = _resolve_static_asset("img/favicon.ico")
    response = FileResponse(file_path.open("rb"), content_type="image/x-icon")
    add_cache_headers(response, str(file_path), request.path)
    return response
