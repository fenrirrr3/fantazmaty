"""Programy dokumentów: formularze i uruchamianie zadań w tle.

Konwersja nigdy nie działa w wątku żądania WWW. Każde wysłanie formularza
tworzy zadanie (core.services.program_jobs). Przeglądarka z JavaScriptem
śledzi postęp przez JSON, a bez JavaScriptu trafia na stronę postępu, która
odświeża się sama i udostępnia plik do pobrania.

Scalanie i podział DOCX oraz stopki tekstów antologii są tylko dla superusera –
sprawdzane po stronie serwera, nie tylko w szablonie.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.filtering import positive_id
from core.models import ProgramPreferences, ReplacementEntry
from core.odkurzacz_forms import DocumentConversionForm, MergeForm, OdkurzaczForm, RepetitionsForm, SplitForm
from core.permissions import is_coordinator, is_superuser, team_member_required
from core.services import program_jobs as jobs

ACTIONS = ('clean', 'convert', 'repetitions', 'merge', 'split')
SUPERUSER_ACTIONS = ('merge', 'split')
FORMS = {'clean': (OdkurzaczForm, None), 'convert': (DocumentConversionForm, 'convert'),
         'repetitions': (RepetitionsForm, 'repetitions'), 'merge': (MergeForm, 'merge'),
         'split': (SplitForm, 'split')}
PROGRAM_NAMES = {'clean': 'Odkurzacz', 'convert': 'Konwerter', 'repetitions': 'Kolorowanie powtórzeń',
                 'merge': 'Scalanie DOCX', 'split': 'Podział DOCX'}
UPLOAD_FIELDS = {'merge': 'documents'}
CREDITS_PARAMETER = 'antologia'


def stored_preferences(user):
    data = ProgramPreferences.objects.filter(user=user).values_list('data', flat=True).first()
    return data if isinstance(data, dict) else {}


def _locked_preferences(user):
    """Rekord ustawień zablokowany do końca transakcji: dwie karty zapisują kolejno, nie nadpisują się."""
    ProgramPreferences.objects.get_or_create(user=user)
    return ProgramPreferences.objects.select_for_update().get(user=user)


def remember_settings(user, action, form):
    """Zapamiętuje ustawienia formularza (bez pliku) przyjętego do pracy – tylko ten jeden program."""
    with transaction.atomic():
        preferences = _locked_preferences(user)
        data = preferences.data if isinstance(preferences.data, dict) else {}
        data[action] = form.settings_values()
        preferences.data = data
        preferences.save(update_fields=['data', 'updated_at'])


def forget_settings(user, action):
    with transaction.atomic():
        preferences = ProgramPreferences.objects.select_for_update().filter(user=user).first()
        if preferences and isinstance(preferences.data, dict) and action in preferences.data:
            del preferences.data[action]
            preferences.save(update_fields=['data', 'updated_at'])


def available_actions(user):
    return [name for name in ACTIONS if name not in SUPERUSER_ACTIONS or is_superuser(user)]


def bound_forms(request, action, preferences=None):
    """Wysłany formularz jest związany z danymi; pozostałe pokazują zapamiętane ustawienia użytkownika."""
    preferences = preferences or {}
    forms = {}
    allowed = available_actions(request.user)
    for name, (form_class, prefix) in FORMS.items():
        if name not in allowed:
            continue
        if name == action:
            forms[name] = form_class(request.POST, request.FILES, prefix=prefix)
        else:
            forms[name] = form_class(prefix=prefix, initial=form_class.initial_from_settings(
                preferences.get(name), prefix=prefix))
    return forms


def label_replacement_rule(forms, count):
    """Etykieta reguły słownikowej odsyła do słownika zamian w CMS."""
    label = f'Własne zamiany słownikowe – słownik zamian (aktywne pozycje: {count})'
    for name in ('clean', 'convert', 'merge'):
        if name not in forms:
            continue
        field = forms[name].fields['rules']
        field.choices = [(key, label if key == 'user_word_corrections' else text) for key, text in field.choices]


def job_options(action, form, user=None):
    data = form.cleaned_data
    if action in ('clean', 'merge'):
        # Scalanie stosuje opcje Odkurzacza do każdego pliku, zawsze z przebudową.
        rebuild = action == 'merge' or data['rebuild']
        options = dict(formats=[], include_docx=True, rebuild=rebuild,
                       normalize=data['normalize_formatting'], justify=data['normalize_formatting'],
                       allow_rebuild_omissions=False, use_cleaner=True,
                       cleaner_rules=data['rules'], remove_soft_whitespace=data['remove_soft_whitespace'],
                       track_changes=data['track_changes'], cleaner_summary=data['cleaner_summary'],
                       keep_page_format=rebuild and data['keep_page_format'],
                       replacements=ReplacementEntry.active_replacements())
        if action == 'merge':
            options['use_template'] = data['use_template']
        else:
            options['remove_fonts'] = data['remove_fonts']
    elif action == 'convert':
        options = dict(formats=data['formats'], use_cleaner=data['use_cleaner'], preserve_filename=True,
                       remove_soft_whitespace=data['use_cleaner'])
        if data['use_cleaner']:
            options.update(cleaner_rules=data['rules'], replacements=ReplacementEntry.active_replacements())
    elif action == 'split':
        options = {}
    else:
        options = dict(formats=[], include_docx=True, normalize=False, repetitions=form.analysis_config())
    # Wyłącznie po stronie serwera: superuser nie ma limitu wielkości analizy
    # powtórzeń, a jego zadanie może trwać dłużej (formularz tego nie ustawia).
    if user is not None and is_superuser(user):
        options.update(analysis_unlimited=True, timeout=jobs.superuser_time_limit())
    return options


def anthology_credits(anthology):
    """Stopki tekstów antologii (bez wycofanych) w kolejności jak na liście tekstów antologii.

    Treść stopki pochodzi z tej samej funkcji co sekcja „Stopka tekstu” na karcie tekstu.
    """
    from core.services.split_mailing import anthology_texts
    from core.supervision import text_credit_groups

    rows = []
    for text in anthology_texts(anthology):
        # Puste role („–” na karcie tekstu) pomijamy: stopka jest do wklejenia w antologii.
        groups = [group for group in text_credit_groups(text) if group['names']]
        lines = [f'{group["label"]}: {", ".join(group["names"])}' for group in groups]
        heading = f'{text.title} – {text.authors_display}' if text.authors_display else text.title
        rows.append({'text': text, 'authors': text.authors_display, 'groups': groups,
                     'copy': '\n'.join([heading, *lines])})
    return rows


def credits_context(request):
    """Sekcja „Stopki tekstów antologii”: wybór antologii (GET) i gotowe stopki."""
    from core.translation_scope import non_abandoned
    from texts.models import Anthology

    raw = request.GET.get(CREDITS_PARAMETER, '').strip()
    context = {'credit_anthologies': non_abandoned(Anthology.objects).order_by('title', 'pk').only('pk', 'title'),
               'credits_open': CREDITS_PARAMETER in request.GET, 'credits_selected': None,
               'credits_rows': [], 'credits_text': '', 'credits_error': ''}
    if not raw:
        return context
    anthology_id = positive_id(raw)
    anthology = context['credit_anthologies'].filter(pk=anthology_id).first() if anthology_id else None
    if anthology is None:
        context['credits_error'] = 'Nie znaleziono wybranej antologii. Wybierz ją ponownie z listy.'
        return context
    rows = anthology_credits(anthology)
    context.update(credits_selected=anthology, credits_rows=rows,
                   credits_text='\n\n'.join(row['copy'] for row in rows))
    return context


def render_programs(request, forms, action=None, *, error='', status=200):
    label_replacement_rule(forms, ReplacementEntry.objects.filter(is_active=True).count())
    superuser = is_superuser(request.user)
    context = {
        'form': forms['clean'], 'conversion_form': forms['convert'], 'repetitions_form': forms['repetitions'],
        'merge_form': forms.get('merge'), 'split_form': forms.get('split'),
        'clean_open': action == 'clean', 'conversion_open': action == 'convert',
        'repetitions_open': action == 'repetitions', 'merge_open': action == 'merge',
        'split_open': action == 'split', 'program_error': error,
        'can_edit_replacements': is_coordinator(request.user),
        'superuser': superuser, 'superuser_time_limit': jobs.superuser_time_limit(),
    }
    if superuser:
        context.update(credits_context(request))
    return render(request, 'core/programs.html', context, status=status)


def forbidden(wants_json):
    if wants_json:
        return JsonResponse({'message': 'Ten program jest dostępny tylko dla superusera.'}, status=403)
    raise PermissionDenied('Ten program jest dostępny tylko dla superusera.')


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def programs(request):
    if request.method == 'GET':
        if CREDITS_PARAMETER in request.GET and not is_superuser(request.user):
            raise PermissionDenied('Stopki tekstów antologii są dostępne tylko dla superusera.')
        # Wejście na stronę sprząta też wygasłe zadania – bez tego niepobrane wyniki
        # czekały na serwerze do następnego zgłoszenia dokumentu.
        jobs.cleanup()
        return render_programs(request, bound_forms(request, None, stored_preferences(request.user)))
    wants_json = request.headers.get('X-Program-Job') == '1'
    action = request.POST.get('program_action', 'clean')
    if action in SUPERUSER_ACTIONS and not is_superuser(request.user):
        return forbidden(wants_json)
    if action == 'reset_settings' and request.POST.get('program') in ACTIONS:
        program = request.POST['program']
        if program in SUPERUSER_ACTIONS and not is_superuser(request.user):
            return forbidden(wants_json)
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
    try:
        token = jobs.create(request, form.cleaned_data[UPLOAD_FIELDS.get(action, 'document')],
                            job_options(action, form, request.user), action)
    except jobs.JobError as error:
        if wants_json:
            return JsonResponse({'message': str(error)}, status=400)
        return render_programs(request, forms, action, error=str(error), status=400)
    except OSError:
        message = 'Nie można zapisać zadania. Administrator musi sprawdzić katalog roboczy programów.'
        if wants_json:
            return JsonResponse({'message': message}, status=503)
        return render_programs(request, forms, action, error=message, status=503)
    # Ustawienia zapamiętujemy dopiero po przyjęciu zadania (pełna kolejka, błąd zapisu – bez zmian).
    if form.settings_fields():
        remember_settings(request.user, action, form)
    url = reverse('core:program_job', args=[token])
    if wants_json:
        return JsonResponse({'url': url}, status=202)
    return redirect(url + '?view=page')
