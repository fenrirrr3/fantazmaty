"""Strona WWW: endpoint z danymi ekipy dla fantazmaty.pl i zakładka System → „Strona WWW”."""
import hmac

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from core.permissions import superuser_required
from core.site_models import SiteTeamSettings
from core.site_team import record_fetch, team_payload, team_sections, team_warnings

JSON_PARAMS = {'ensure_ascii': False}


def _json(data, status=200):
    response = JsonResponse(data, status=status, json_dumps_params=JSON_PARAMS)
    response['Cache-Control'] = 'no-store'
    return response


@never_cache
@require_GET
def site_team_api(request):
    """GET bez logowania; wymaga nagłówka X-Site-Key równego SITE_API_KEY. Brak klucza w konfiguracji – 503."""
    key = settings.SITE_API_KEY
    if not key:
        return _json({'blad': 'Brak klucza SITE_API_KEY w konfiguracji.'}, status=503)
    provided = request.headers.get('X-Site-Key', '')
    if not hmac.compare_digest(provided.encode(), key.encode()):
        return _json({'blad': 'Brak dostępu.'}, status=403)
    payload = team_payload()
    record_fetch(payload['wersja'])
    return _json(payload)


class SiteTeamSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteTeamSettings
        fields = ('lead_editor', 'site_person')


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
def site_www(request):
    """Karta „Ekipa”: ustawienia, podgląd danych dla strony i ostrzeżenia; ``?format=json`` – sam JSON."""
    team_settings = SiteTeamSettings.load()
    if request.GET.get('format') == 'json':
        return _json(team_payload())
    form = SiteTeamSettingsForm(request.POST or None, instance=team_settings)
    if request.method == 'POST':
        if form.is_valid():
            form.save()
            messages.success(request, 'Ustawienia strony zapisane.')
            return redirect('core:site_www')
    sections = team_sections(include_disabled=True)
    return render(request, 'core/site_www.html', {
        'form': form, 'sections': sections, 'warnings': team_warnings(sections),
        'team_settings': team_settings, 'has_api_key': bool(settings.SITE_API_KEY),
    }, status=400 if form.errors else 200)
