"""Strona WWW: endpointy dla fantazmaty.pl (ekipa, katalog szukajki, nagrania strony „Słuchaj”) i zakładki
System → „Ekipa WWW”, „Szukajka WWW”, „Słuchaj WWW”."""
import hmac
import json

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from core.permissions import superuser_required
from core.site_catalog import (PLAN_SALT, apply_add, apply_update, apply_update_all, candidate_anthologies,
                               catalog_anthologies, catalog_counts, catalog_payload, catalog_warnings, count_text,
                               plan_add, plan_digest, plan_update, plan_update_all, plans_digest, record_catalog_fetch,
                               story_entry, update_blocked_reason)
from core.site_listen import (add_anthology, candidate_anthologies as listen_candidates, count_text as recording_text,
                              link_listen_to_cms, listen_counts, listen_data, listen_payload, record_listen_fetch)
from core.site_models import SiteCatalogAnthology, SiteTeamSettings, validate_site_url
from core.site_team import record_fetch, team_payload, team_sections, team_warnings
from texts.models import Anthology

JSON_PARAMS = {'ensure_ascii': False}


def _json(data, status=200):
    response = JsonResponse(data, status=status, json_dumps_params=JSON_PARAMS)
    response['Cache-Control'] = 'no-store'
    return response


def _site_api(request, build_payload, record):
    """GET bez logowania; wymaga nagłówka X-Site-Key równego SITE_API_KEY. Brak klucza w konfiguracji – 503."""
    key = settings.SITE_API_KEY
    if not key:
        return _json({'blad': 'Brak klucza SITE_API_KEY w konfiguracji.'}, status=503)
    provided = request.headers.get('X-Site-Key', '')
    if not hmac.compare_digest(provided.encode(), key.encode()):
        return _json({'blad': 'Brak dostępu.'}, status=403)
    payload = build_payload()
    record(payload['wersja'])
    return _json(payload)


@never_cache
@require_GET
def site_team_api(request):
    return _site_api(request, team_payload, record_fetch)


@never_cache
@require_GET
def site_catalog_api(request):
    return _site_api(request, catalog_payload, record_catalog_fetch)


@never_cache
@require_GET
def site_listen_api(request):
    return _site_api(request, listen_payload, record_listen_fetch)


class SiteTeamSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteTeamSettings
        fields = ('lead_editor', 'site_person')


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
def site_www(request):
    """Karta „Ekipa WWW”: ustawienia, podgląd danych dla strony i ostrzeżenia; ``?format=json`` – sam JSON."""
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


# --- Szukajka WWW ---------------------------------------------------------------------------------------------------

class SiteCatalogSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteTeamSettings
        fields = ('catalog_page_url',)


class CatalogAddForm(forms.Form):
    """Dodanie antologii z CMS-u do katalogu: wybór spośród gotowych antologii spoza katalogu i adres strony."""
    anthology = forms.ModelChoiceField(label='Antologia z CMS-u', queryset=Anthology.objects.none(),
                                       empty_label='– wybierz –')
    url = forms.URLField(label='Adres antologii na stronie', max_length=500, assume_scheme='https',
                         help_text='https://fantazmaty.pl/czytaj/antologie/…/')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['anthology'].queryset = candidate_anthologies()

    def clean_url(self):
        url = self.cleaned_data['url']
        if not url.startswith('https://fantazmaty.pl/'):
            raise forms.ValidationError('Adres musi zaczynać się od https://fantazmaty.pl/.')
        return url


def _plan_token(plan, digest=plan_digest):
    return signing.dumps(digest(plan), salt=PLAN_SALT)


def _plan_matches(token, plan, digest=plan_digest):
    try:
        return signing.loads(token or '', salt=PLAN_SALT, max_age=settings.SESSION_COOKIE_AGE) == digest(plan)
    except signing.BadSignature:
        return False


def _catalog_rows():
    rows = []
    for item in catalog_anthologies(include_disabled=True):
        stories = list(item.stories.all())
        anthology = item.anthology
        rows.append({'item': item, 'stories': len(stories), 'novel': bool(anthology and anthology.is_novel),
                     'linked_stories': len(stories) if anthology and anthology.is_novel
                     else sum(1 for s in stories if s.text_id), 'blocked': update_blocked_reason(item)})
    return rows


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
def site_catalog(request):
    """Karta „Szukajka WWW”: liczniki, adres szukajki, dodanie antologii z CMS-u (podgląd → dodanie), tabela
    katalogu z przyciskami aktualizacji i ostrzeżenia; ``?format=json`` – sam JSON."""
    site_settings = SiteTeamSettings.load()
    if request.GET.get('format') == 'json':
        return _json(catalog_payload())
    action = request.POST.get('action', '') if request.method == 'POST' else ''
    settings_form = SiteCatalogSettingsForm(request.POST if action == 'settings' else None, instance=site_settings)
    add_form = CatalogAddForm(request.POST if action in ('preview', 'add') else None)
    add_plan, status = None, 200
    if action == 'settings':
        if settings_form.is_valid():
            settings_form.save()
            messages.success(request, 'Adres szukajki zapisany.')
            return redirect('core:site_catalog')
        status = 400
    elif action in ('preview', 'add'):
        if add_form.is_valid():
            anthology = add_form.cleaned_data['anthology']
            add_plan = plan_add(anthology)  # kandydaci formularza są już Gotowi i spoza Ekstraktów
            if action == 'add':
                if _plan_matches(request.POST.get('plan'), add_plan):
                    added = apply_add(anthology, add_form.cleaned_data['url'], add_plan)
                    messages.success(request, f'Dodano antologię „{added.title}” '
                                              f'({count_text(len(add_plan["stories"]), STORY_FORMS)}).')
                    return redirect('core:site_catalog')
                messages.error(request, 'Dane w CMS-ie zmieniły się od podglądu – sprawdź je ponownie.')
                status = 409
        else:
            status = 400
    payload = catalog_payload()
    return render(request, 'core/site_catalog.html', {
        'settings_form': settings_form, 'add_form': add_form, 'add_plan': add_plan,
        'add_plan_token': _plan_token(add_plan) if add_plan else '',
        'counts': payload['liczby'], 'counts_text': _counts_text(payload['liczby']),
        'rows': _catalog_rows(), 'warnings': catalog_warnings(), 'site_settings': site_settings,
        'has_api_key': bool(settings.SITE_API_KEY), 'has_candidates': candidate_anthologies().exists(),
    }, status=status)


STORY_FORMS = ('opowiadanie', 'opowiadania', 'opowiadań')
ANTHOLOGY_FORMS = ('antologia', 'antologie', 'antologii')
IN_ANTHOLOGY_FORMS = ('antologii', 'antologiach', 'antologiach')
GENRE_FORMS = ('gatunek', 'gatunki', 'gatunków')
TAG_FORMS = ('tag', 'tagi', 'tagów')


def _counts_text(counts):
    return f'{count_text(counts["opowiadania"], STORY_FORMS)} · {count_text(counts["antologie"], ANTHOLOGY_FORMS)}'


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
def site_catalog_update(request, pk):
    """Podgląd różnic między katalogiem a CMS-em dla jednej antologii; POST „Zastosuj” dopisuje nowe
    opowiadania, gatunki i tagi (bez usuwania i nadpisywania)."""
    with transaction.atomic():
        return _site_catalog_update(request, pk)


def _site_catalog_update(request, pk):
    # POST blokuje wpis katalogu: drugie równoczesne „Zastosuj” czeka i liczy plan od nowa (bez duplikatów).
    queryset = SiteCatalogAnthology.objects.select_related('anthology')
    if request.method == 'POST':
        queryset = queryset.select_for_update()
    catalog_anthology = get_object_or_404(queryset, pk=pk)
    try:
        plan = plan_update(catalog_anthology)
    except ValueError as error:
        messages.error(request, f'„{catalog_anthology.title}”: {error}')
        return redirect('core:site_catalog')
    status = 200
    if request.method == 'POST':
        if not plan['has_changes']:
            messages.info(request, f'„{catalog_anthology.title}”: brak zmian do zastosowania.')
            return redirect('core:site_catalog')
        if _plan_matches(request.POST.get('plan'), plan):
            result = apply_update(plan)
            messages.success(request, f'„{catalog_anthology.title}”: dodano {count_text(result["added"], STORY_FORMS)}, '
                                      f'uzupełniono gatunki lub tagi w {result["updated"]}.')
            return redirect('core:site_catalog')
        messages.error(request, 'Dane w CMS-ie zmieniły się od podglądu – sprawdź je ponownie.')
        status = 409
    return render(request, 'core/site_catalog_update.html', {
        'catalog_anthology': catalog_anthology, 'plan': plan, 'plan_token': _plan_token(plan),
        'current_stories': [story_entry(story) for story in catalog_anthology.stories.all()],
        'counts': catalog_counts([{'opowiadania': [story_entry(s) for s in catalog_anthology.stories.all()]}]),
    }, status=status)


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
def site_catalog_update_all(request):
    """Zbiorczy podgląd różnic katalogu z CMS-em dla wszystkich antologii, które da się aktualizować; POST
    „Zastosuj wszystkie” stosuje wszystkie plany w jednej transakcji, jeśli dane nie zmieniły się od podglądu."""
    status = 200
    with transaction.atomic():
        if request.method == 'POST':
            # Jak przy pojedynczej aktualizacji: równoczesne „Zastosuj wszystkie” wykonują się po kolei.
            list(SiteCatalogAnthology.objects.select_for_update().values_list('pk', flat=True))
        overview = plan_update_all()
        if request.method == 'POST':
            changed = overview['changed']
            if not changed:
                messages.info(request, 'Brak zmian do zastosowania – katalog jest zgodny z CMS-em.')
                return redirect('core:site_catalog')
            if _plan_matches(request.POST.get('plan'), changed, plans_digest):
                totals = apply_update_all(changed)
                messages.success(request, (
                    f'Zaktualizowano katalog w {count_text(totals["anthologies"], IN_ANTHOLOGY_FORMS)}: dodano '
                    f'{count_text(totals["added"], STORY_FORMS)}, dopisano {count_text(totals["genres"], GENRE_FORMS)} '
                    f'i {count_text(totals["tags"], TAG_FORMS)}.'))
                return redirect('core:site_catalog')
            messages.error(request, 'Dane w CMS-ie zmieniły się od podglądu – sprawdź je ponownie.')
            status = 409
    for plan in overview['changed'] + overview['unchanged']:
        plan['notes'] = len(plan['reports']) + len(plan['missing']) + len(plan['unlinked'])
    return render(request, 'core/site_catalog_update_all.html', {
        'overview': overview, 'plan_token': _plan_token(overview['changed'], plans_digest),
    }, status=status)


# --- Słuchaj WWW ----------------------------------------------------------------------------------------------------

class SiteListenSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteTeamSettings
        fields = ('listen_page_url',)


class ListenAddForm(forms.Form):
    """Dodanie antologii CMS-u z opublikowanymi audiobookami na koniec strony „Słuchaj”."""
    anthology = forms.ModelChoiceField(label='Antologia z CMS-u', queryset=Anthology.objects.none(),
                                       empty_label='– wybierz –')
    cover_url = forms.URLField(label='Adres okładki', max_length=500, assume_scheme='https',
                               validators=[validate_site_url],
                               help_text='https://fantazmaty.pl/wp-content/uploads/…')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['anthology'].queryset = listen_candidates()


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
def site_listen(request):
    """Karta „Słuchaj WWW”: liczniki, adres strony, dodanie antologii z CMS-u, ponowne dopasowanie nagrań z dawnej
    strony, ostrzeżenia i podgląd JSON-u; ``?format=json`` – sam JSON."""
    site_settings = SiteTeamSettings.load()
    if request.GET.get('format') == 'json':
        return _json(listen_payload())
    action = request.POST.get('action', '') if request.method == 'POST' else ''
    settings_form = SiteListenSettingsForm(request.POST if action == 'settings' else None, instance=site_settings)
    add_form = ListenAddForm(request.POST if action == 'add' else None)
    status = 200
    if action == 'settings':
        if settings_form.is_valid():
            settings_form.save()
            messages.success(request, 'Adres strony zapisany.')
            return redirect('core:site_listen')
        status = 400
    elif action == 'add':
        if add_form.is_valid():
            added = add_anthology(add_form.cleaned_data['anthology'], add_form.cleaned_data['cover_url'])
            messages.success(request, f'Dodano antologię „{added.title}” na koniec strony.')
            return redirect('core:site_listen')
        status = 400
    elif action == 'relink':
        with transaction.atomic():
            linked = link_listen_to_cms()
        messages.success(request, f'Dopasowano: {count_text(linked["antologie"], ANTHOLOGY_FORMS)}, '
                                  f'{recording_text(linked["nagrania"])}.')
        return redirect('core:site_listen')
    elif action:
        status = 400
    data = listen_data()
    payload = listen_payload(data)
    return render(request, 'core/site_listen.html', {
        'settings_form': settings_form, 'add_form': add_form, 'counts': data['liczniki'],
        'counts_text': recording_text(data['liczniki']['nagrania']), 'warnings': data['ostrzezenia'],
        'rows': [{'item': item, 'counts': listen_counts([item])} for item in data['antologie']], 'preview': json.dumps(payload, ensure_ascii=False, indent=2),
        'site_settings': site_settings, 'has_api_key': bool(settings.SITE_API_KEY),
        'has_candidates': listen_candidates().exists(),
    }, status=status)
