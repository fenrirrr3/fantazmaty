from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from core.permissions import post_layout_required, team_member_required


@never_cache
@login_required
@require_GET
@post_layout_required
def post_layout(request):
    return render(request, 'core/proofreading_section.html', {'title': 'Korekta poskładowa'})


@never_cache
@login_required
@require_GET
@team_member_required
def audio_proofreading(request):
    return render(request, 'core/proofreading_section.html', {'title': 'Korekta audiobooków'})
