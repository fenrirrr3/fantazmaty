from django.contrib.auth.decorators import login_required
from django.http import FileResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.permissions import team_member_required
from core.odkurzacz_forms import OdkurzaczForm, DocumentConversionForm, RepetitionsForm
from core.services import program_jobs as jobs
from core.services.pending_documents import load_pending, PendingDocumentError


def start(request):
    action = request.POST.get('program_action', 'clean')
    pending_path = None
    try:
        if action in ('clean', 'clean_confirm'):
            if action == 'clean_confirm':
                upload, pending, pending_path = load_pending(request, request.POST.get('rebuild_token', ''))
                form = OdkurzaczForm(pending['options'], {'document': upload})
            else:
                form = OdkurzaczForm(request.POST, request.FILES)
        elif action == 'convert':
            form = DocumentConversionForm(request.POST, request.FILES, prefix='convert')
        elif action == 'repetitions':
            form = RepetitionsForm(request.POST, request.FILES, prefix='repetitions')
        else:
            raise jobs.JobError('Wybierz program.')
        if not form.is_valid():
            return JsonResponse({'errors': {key: [str(message) for message in values] for key, values in form.errors.items()}}, status=400)
        data = form.cleaned_data
        if action in ('clean', 'clean_confirm'):
            options = dict(formats=[], include_docx=True, rebuild=data['rebuild'],
                           normalize=data['normalize_formatting'], justify=data['normalize_formatting'],
                           allow_rebuild_omissions=action == 'clean_confirm', use_cleaner=True,
                           cleaner_rules=data['rules'], remove_soft_whitespace=data['remove_soft_whitespace'])
        elif action == 'convert':
            options = dict(formats=data['formats'], use_cleaner=data['use_cleaner'], preserve_filename=True,
                           remove_soft_whitespace=data['remove_soft_whitespace'])
        else:
            options = dict(formats=[], include_docx=True, normalize=False, repetitions=form.analysis_config())
        token = jobs.create(request, data['document'], options, 'clean' if action == 'clean_confirm' else action)
        if pending_path is not None:
            pending_path.unlink(missing_ok=True)
        return JsonResponse({'url': reverse('core:program_job', args=[token])}, status=202)
    except (jobs.JobError, PendingDocumentError) as error:
        return JsonResponse({'message': str(error)}, status=400)
    except OSError:
        return JsonResponse({'message': 'Nie można zapisać zadania. Administrator musi sprawdzić katalog roboczy programów.'}, status=503)


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def program_job(request, token):
    try:
        folder = jobs.resolve(request, token)
        state = jobs.status(folder)
        if request.method == 'POST':
            action = request.POST.get('action')
            if action == 'cancel':
                if state['state'] in ('queued', 'running', 'confirmation'):
                    (folder / 'cancel').touch()
                return JsonResponse({'state': 'cancelling'} if state['state'] in ('queued', 'running') else jobs.status(folder))
            if action == 'confirm':
                renewed = jobs.refreshed_token(token)
                jobs.confirm(folder)
                return JsonResponse({**jobs.status(folder), 'url': reverse('core:program_job', args=[renewed])}, status=202)
            raise jobs.JobError('Nieprawidłowa akcja.')
        if request.GET.get('download') == '1':
            if state['state'] != 'done':
                raise jobs.JobError('Plik nie jest jeszcze gotowy do pobrania.')
            response = FileResponse((folder / 'result').open('rb'), as_attachment=True,
                                    filename=state['filename'], content_type=state['mime'])
            response['Cache-Control'] = 'private, no-store'
            response['X-Content-Type-Options'] = 'nosniff'
            return response
        return JsonResponse(state)
    except jobs.JobError as error:
        return JsonResponse({'message': str(error)}, status=404)
    except (OSError, ValueError):
        return JsonResponse({'message': 'Nie można odczytać zadania. Spróbuj ponownie.'}, status=503)
