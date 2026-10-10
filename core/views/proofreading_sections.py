from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods
from core.permissions import post_layout_required, team_member_required


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@post_layout_required
def post_layout(request):
    from django.contrib import messages
    from django.core.exceptions import ValidationError, PermissionDenied
    from django.db.models import Q
    from core.models import PostLayoutAssignment
    from core.post_layout import AssignmentForm, can_manage, create_assignment, change_status, StaleAssignment
    from core.pagination import paginate_items
    manager = can_manage(request.user)
    action = request.POST.get('action') if request.method == 'POST' else None
    form = AssignmentForm(request.POST if action == 'create' else None, user=request.user) if manager else None
    errors, code = [], 200
    if request.method == 'POST':
        try:
            if action == 'create':
                if not manager:
                    raise PermissionDenied()
                if form.is_valid():
                    create_assignment(user=request.user, **form.cleaned_data)
                    messages.success(request, 'Dodano przydział korekty poskładowej.')
                    return redirect('core:post_layout')
                code = 400
            elif action == 'status':
                raw = request.POST.get('pk', '')
                if not raw.isascii() or not raw.isdecimal() or len(raw) > 18:
                    raise ValidationError('Wybierz prawidłowy wpis.')
                change_status(user=request.user, pk=int(raw), status=request.POST.get('status'), version=request.POST.get('version'))
                messages.success(request, 'Zapisano status i daty etapu.')
                return redirect(request.path + ('?' + request.GET.urlencode() if request.GET else ''))
            else:
                raise ValidationError('Nieznana operacja.')
        except ValidationError as exc:
            errors, code = exc.messages, 409 if isinstance(exc, StaleAssignment) else 400
    rows = PostLayoutAssignment.objects.select_related('anthology', 'proofreader__person_profile')
    if not manager:
        rows = rows.filter(proofreader=request.user)
    selected = request.GET.get('assignment', '')
    if selected:
        rows = rows.filter(pk=int(selected)) if selected.isascii() and selected.isdecimal() and len(selected) <= 18 else rows.none()
    query = request.GET.get('q', '').strip()[:200]
    if query:
        rows = rows.filter(Q(anthology__title__plcontains=query) | Q(proofreader__person_profile__first_name__plcontains=query)
            | Q(proofreader__person_profile__last_name__plcontains=query))
    hide_completed = request.GET.get('hide_completed') == '1'
    if hide_completed:
        rows = rows.exclude(status=PostLayoutAssignment.Status.COMPLETED)
    page = paginate_items(request, rows)
    return render(request, 'core/post_layout.html', {'assignments': page, 'page_obj': page, 'form': form,
        'can_manage': manager, 'errors': errors, 'hide_completed': hide_completed, 'query': query}, status=code)


@never_cache
@login_required
@require_GET
@team_member_required
def audio_proofreading(request):
    from core.views.audiobook_production import list_page
    return list_page(request, proofreading=True)
