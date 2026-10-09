"""Programy dokumentów: formularze i uruchamianie zadań w tle.

Konwersja nigdy nie działa w wątku żądania WWW. Każde wysłanie formularza
tworzy zadanie (core.services.program_jobs). Przeglądarka z JavaScriptem
śledzi postęp przez JSON, a bez JavaScriptu trafia na stronę postępu, która
odświeża się sama i udostępnia plik do pobrania.
"""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.odkurzacz_forms import DocumentConversionForm, OdkurzaczForm, RepetitionsForm
from core.permissions import team_member_required
from core.services import program_jobs as jobs

ACTIONS = ('clean', 'convert', 'repetitions')


def bound_forms(request, action):
    """The submitted form is bound; the other two stay empty."""
    return {
        'clean': OdkurzaczForm(request.POST if action == 'clean' else None,
                               request.FILES if action == 'clean' else None),
        'convert': DocumentConversionForm(request.POST if action == 'convert' else None,
                                          request.FILES if action == 'convert' else None, prefix='convert'),
        'repetitions': RepetitionsForm(request.POST if action == 'repetitions' else None,
                                       request.FILES if action == 'repetitions' else None, prefix='repetitions'),
    }


def job_options(action, form):
    data = form.cleaned_data
    if action == 'clean':
        return dict(formats=[], include_docx=True, rebuild=data['rebuild'],
                    normalize=data['normalize_formatting'], justify=data['normalize_formatting'],
                    allow_rebuild_omissions=False, use_cleaner=True,
                    cleaner_rules=data['rules'], remove_soft_whitespace=data['remove_soft_whitespace'])
    if action == 'convert':
        return dict(formats=data['formats'], use_cleaner=data['use_cleaner'], preserve_filename=True,
                    remove_soft_whitespace=data['remove_soft_whitespace'])
    return dict(formats=[], include_docx=True, normalize=False, repetitions=form.analysis_config())


def render_programs(request, forms, action=None, *, error='', status=200):
    return render(request, 'core/programs.html', {
        'form': forms['clean'], 'conversion_form': forms['convert'], 'repetitions_form': forms['repetitions'],
        'clean_open': action == 'clean', 'conversion_open': action == 'convert',
        'repetitions_open': action == 'repetitions', 'program_error': error,
    }, status=status)


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def programs(request):
    if request.method == 'GET':
        return render_programs(request, bound_forms(request, None))
    wants_json = request.headers.get('X-Program-Job') == '1'
    action = request.POST.get('program_action', 'clean')
    if action not in ACTIONS:
        if wants_json:
            return JsonResponse({'message': 'Wybierz program.'}, status=400)
        return render_programs(request, bound_forms(request, None), error='Wybierz narzędzie i wyślij formularz ponownie.', status=400)
    forms = bound_forms(request, action)
    form = forms[action]
    if not form.is_valid():
        if wants_json:
            return JsonResponse({'errors': {key: [str(message) for message in values] for key, values in form.errors.items()}}, status=400)
        return render_programs(request, forms, action)
    try:
        token = jobs.create(request, form.cleaned_data['document'], job_options(action, form), action)
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
