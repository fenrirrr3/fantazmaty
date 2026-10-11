"""Programy dokumentów: formularze i uruchamianie zadań w tle.

Konwersja nigdy nie działa w wątku żądania WWW. Każde wysłanie formularza
tworzy zadanie (core.services.program_jobs). Przeglądarka z JavaScriptem
śledzi postęp przez JSON, a bez JavaScriptu trafia na stronę postępu, która
odświeża się sama i udostępnia plik do pobrania.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.models import ProgramPreferences, ReplacementEntry
from core.odkurzacz_forms import DocumentConversionForm, OdkurzaczForm, RepetitionsForm
from core.permissions import is_coordinator, is_superuser, team_member_required
from core.services import program_jobs as jobs

ACTIONS = ('clean', 'convert', 'repetitions')
FORMS = {'clean': (OdkurzaczForm, None), 'convert': (DocumentConversionForm, 'convert'),
         'repetitions': (RepetitionsForm, 'repetitions')}
PROGRAM_NAMES = {'clean': 'Odkurzacz', 'convert': 'Konwerter', 'repetitions': 'Kolorowanie powtórzeń'}


def stored_preferences(user):
    data = ProgramPreferences.objects.filter(user=user).values_list('data', flat=True).first()
    return data if isinstance(data, dict) else {}


def remember_settings(user, action, form):
    """Zapamiętuje ustawienia poprawnie wysłanego formularza (bez pliku)."""
    preferences, _ = ProgramPreferences.objects.get_or_create(user=user)
    data = preferences.data if isinstance(preferences.data, dict) else {}
    data[action] = form.settings_values()
    preferences.data = data
    preferences.save(update_fields=['data', 'updated_at'])


def forget_settings(user, action):
    preferences = ProgramPreferences.objects.filter(user=user).first()
    if preferences and isinstance(preferences.data, dict) and action in preferences.data:
        del preferences.data[action]
        preferences.save(update_fields=['data', 'updated_at'])


def bound_forms(request, action, preferences=None):
    """The submitted form is bound; the other two show the user's remembered settings."""
    preferences = preferences or {}
    forms = {}
    for name, (form_class, prefix) in FORMS.items():
        if name == action:
            forms[name] = form_class(request.POST, request.FILES, prefix=prefix)
        else:
            forms[name] = form_class(prefix=prefix, initial=form_class.initial_from_settings(
                preferences.get(name), prefix=prefix))
    return forms


def label_replacement_rule(forms, count):
    """Etykieta reguły słownikowej odsyła do słownika zamian w CMS."""
    label = f'Własne zamiany słownikowe – słownik zamian (aktywne pozycje: {count})'
    for name in ('clean', 'convert'):
        field = forms[name].fields['rules']
        field.choices = [(key, label if key == 'user_word_corrections' else text) for key, text in field.choices]


def job_options(action, form, user=None):
    data = form.cleaned_data
    if action == 'clean':
        options = dict(formats=[], include_docx=True, rebuild=data['rebuild'],
                       normalize=data['normalize_formatting'], justify=data['normalize_formatting'],
                       allow_rebuild_omissions=False, use_cleaner=True,
                       cleaner_rules=data['rules'], remove_soft_whitespace=data['remove_soft_whitespace'],
                       track_changes=data['track_changes'], cleaner_summary=data['cleaner_summary'],
                       keep_page_format=data['rebuild'] and data['keep_page_format'],
                       replacements=ReplacementEntry.active_replacements())
    elif action == 'convert':
        options = dict(formats=data['formats'], use_cleaner=data['use_cleaner'], preserve_filename=True,
                       remove_soft_whitespace=data['use_cleaner'])
        if data['use_cleaner']:
            options.update(cleaner_rules=data['rules'], replacements=ReplacementEntry.active_replacements())
    else:
        options = dict(formats=[], include_docx=True, normalize=False, repetitions=form.analysis_config())
    # Wyłącznie po stronie serwera: superuser nie ma limitu wielkości analizy
    # powtórzeń, a jego zadanie może trwać dłużej (formularz tego nie ustawia).
    if user is not None and is_superuser(user):
        options.update(analysis_unlimited=True, timeout=jobs.superuser_time_limit())
    return options


def render_programs(request, forms, action=None, *, error='', status=200):
    label_replacement_rule(forms, ReplacementEntry.objects.filter(is_active=True).count())
    return render(request, 'core/programs.html', {
        'form': forms['clean'], 'conversion_form': forms['convert'], 'repetitions_form': forms['repetitions'],
        'clean_open': action == 'clean', 'conversion_open': action == 'convert',
        'repetitions_open': action == 'repetitions', 'program_error': error,
        'can_edit_replacements': is_coordinator(request.user),
        'superuser_limits': is_superuser(request.user), 'superuser_time_limit': jobs.superuser_time_limit(),
    }, status=status)


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def programs(request):
    if request.method == 'GET':
        # Wejście na stronę sprząta też wygasłe zadania – bez tego niepobrane wyniki
        # czekały na serwerze do następnego zgłoszenia dokumentu.
        jobs.cleanup()
        return render_programs(request, bound_forms(request, None, stored_preferences(request.user)))
    wants_json = request.headers.get('X-Program-Job') == '1'
    action = request.POST.get('program_action', 'clean')
    if action == 'reset_settings' and request.POST.get('program') in ACTIONS:
        program = request.POST['program']
        forget_settings(request.user, program)
        messages.success(request, f'{PROGRAM_NAMES[program]}: przywrócono ustawienia domyślne.')
        return redirect(reverse('core:programs') + f'#program-{program}')
    if action not in ACTIONS:
        if wants_json:
            return JsonResponse({'message': 'Wybierz program.'}, status=400)
        return render_programs(request, bound_forms(request, None, stored_preferences(request.user)),
                               error='Wybierz narzędzie i wyślij formularz ponownie.', status=400)
    forms = bound_forms(request, action, stored_preferences(request.user))
    form = forms[action]
    if not form.is_valid():
        if wants_json:
            return JsonResponse({'errors': {key: [str(message) for message in values] for key, values in form.errors.items()}}, status=400)
        return render_programs(request, forms, action)
    remember_settings(request.user, action, form)
    try:
        token = jobs.create(request, form.cleaned_data['document'], job_options(action, form, request.user), action)
    except jobs.JobError as error:
        if wants_json:
            return JsonResponse({'message': str(error)}, status=400)
        return render_programs(request, forms, action, error=str(error), status=400)
    except OSError:
        message = 'Nie można zapisać zadania. Administrator musi sprawdzić katalog roboczy programów.'
        if wants_json:
            return JsonResponse({'message': message}, status=503)
        return render_programs(request, forms, action, error=message, status=503)
    url = reverse('core:program_job', args=[token])
    if wants_json:
        return JsonResponse({'url': url}, status=202)
    return redirect(url + '?view=page')
