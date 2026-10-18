"""Social Media – zalążek narzędzia promocyjnego (na razie pusta zakładka)."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.permissions import coordinator_required


@never_cache
@login_required
@require_GET
@coordinator_required
def social_media(request):
    return render(request, "core/social_media.html")
