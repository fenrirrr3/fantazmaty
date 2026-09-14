from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_GET
from core.permissions import team_member_required

@login_required
@require_GET
@team_member_required
def programs(request):
    return render(request, "core/programs.html")
