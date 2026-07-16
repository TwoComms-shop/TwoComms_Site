import mimetypes

from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from .models import UserProfile


@login_required
@require_GET
def ubd_document(request, profile_id):
    profile = get_object_or_404(UserProfile, pk=profile_id)
    if profile.user_id != request.user.pk and not request.user.is_staff:
        raise Http404
    if not profile.ubd_doc:
        raise Http404

    try:
        document = profile.ubd_doc.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404

    content_type = mimetypes.guess_type(profile.ubd_doc.name)[0] or 'application/octet-stream'
    response = FileResponse(
        document,
        content_type=content_type,
        as_attachment=False,
        filename=f'ubd-document{mimetypes.guess_extension(content_type) or ""}',
    )
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return response
