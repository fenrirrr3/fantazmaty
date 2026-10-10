"""Status, potwierdzenie, anulowanie i pobranie wyniku zadania programu."""
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.permissions import team_member_required
from core.services import program_jobs as jobs

ACTIVE_STATES = ('queued', 'running')


def _page_url(token):
    return reverse('core:program_job', args=[token]) + '?view=page'


def _render_page(request, token, state, *, status=200):
    return render(request, 'core/program_job.html', {
        'state': state, 'token': token,
        'refresh': state.get('state') in ACTIVE_STATES + ('cancelling',),
        'download_url': reverse('core:program_job', args=[token]) + '?download=1',
    }, status=status)


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def program_job(request, token):
    download = request.method == 'GET' and request.GET.get('download') == '1'
    # Pobranie pliku otwiera się w przeglądarce, więc błędy pokazujemy jako stronę.
    page = download or request.GET.get('view') == 'page' or request.POST.get('view') == 'page'
    try:
        folder = jobs.resolve(request, token)
        state = jobs.status(folder)
        if request.method == 'POST':
            action = request.POST.get('action')
            if action == 'cancel':
                if state['state'] in ACTIVE_STATES + ('confirmation',):
                    (folder / 'cancel').touch()
                if page:
                    return redirect(_page_url(token))
                return JsonResponse({'state': 'cancelling'} if state['state'] in ACTIVE_STATES else jobs.status(folder))
            if action == 'confirm':
                renewed = jobs.refreshed_token(token)
                jobs.confirm(folder)
                if page:
                    return redirect(_page_url(renewed))
                return JsonResponse({**jobs.status(folder), 'url': reverse('core:program_job', args=[renewed])}, status=202)
            raise jobs.JobError('Nieprawidłowa akcja.')
        if download:
            if state['state'] != 'done':
                raise jobs.JobError('Plik nie jest jeszcze gotowy do pobrania.')
            response = FileResponse((folder / 'result').open('rb'), as_attachment=True,
                                    filename=state['filename'], content_type=state['mime'])
            response['Cache-Control'] = 'private, no-store'
            response['X-Content-Type-Options'] = 'nosniff'
            # Po wysłaniu pliku usuwamy zadanie i wynik z serwera (najpierw
            # zamyka się plik, potem katalog – kolejność zamknięć Django).
            response._resource_closers.append(lambda: jobs.remove(folder))
            return response
        if page:
            return _render_page(request, token, state)
        return JsonResponse(state)
    except jobs.JobError as error:
        if download and str(error).startswith('Zadanie wygasło'):
            error = jobs.JobError('Plik został już pobrany i usunięty z serwera albo zadanie wygasło. '
                                  'Uruchom program ponownie, jeśli potrzebujesz kolejnej kopii.')
        if page:
            return _render_page(request, token, {'state': 'error', 'message': str(error)}, status=404)
        return JsonResponse({'message': str(error)}, status=404)
    except (OSError, ValueError):
        message = 'Nie można odczytać zadania. Spróbuj ponownie.'
        if page:
            return _render_page(request, token, {'state': 'error', 'message': message}, status=503)
        return JsonResponse({'message': message}, status=503)
