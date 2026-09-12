from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from core.permissions import coordinator_required, superuser_required
from core.filtering import facet_queryset
from core.pagination import paginate_items
from core.intake_forms import ExtractForm, RecruitmentForm, SingleReviewForm
from core.models import Recruitment
from texts.models import Extract
from texts.blacklist import apply_blacklist


def _list(request, model, form_class, title, route, fields):
    query = request.GET.get('q', '').strip()[:500]
    statuses = [v for v in request.GET.getlist('status') if v in model.Status.values]
    items = model.objects.all()
    for term in query.split():
        condition = Q()
        for field in fields:
            condition |= Q(**{f'{field}__plcontains': term})
        items = items.filter(condition)
    blacklist = request.GET.get('blacklist', '')
    if model is Extract:
        items = items.select_related('author')
        recruitment = request.GET.get('recruitment', '').strip()
    else:
        recruitment = ''
    dimensions = {'status': ('status', statuses)}
    if model is Extract:
        dimensions['recruitment'] = ('recruitment', [recruitment] if recruitment else [])
    else:
        department = request.GET.get('department', '')
        dimensions['department'] = ('department', [department] if department in model.Department.values else [])
    items, facets = facet_queryset(items, dimensions)
    page = paginate_items(request, items)
    return render(request, 'core/intake_list.html', {
        'title': title, 'route': route, 'is_extract': model is Extract, 'items': page, 'page_obj': page,
        'query': query, 'status_choices': [(v, label) for v, label in model.Status.choices if v in facets['status']], 'selected_statuses': statuses,
        'blacklist': blacklist, 'recruitment': recruitment,
        'selected_department': request.GET.get('department', ''),
        'departments': [(v, label) for v, label in model.Department.choices if v in facets['department']] if model is Recruitment else [],
        'recruitments': sorted(facets['recruitment'], key=str.casefold) if model is Extract else [],
    })


def _edit(request, model, form_class, title, route, pk):
    instance = get_object_or_404(model, pk=pk) if pk else None
    form = form_class(request.POST if request.method == 'POST' else None, instance=instance)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            if instance:
                locked = model.objects.select_for_update().get(pk=pk)
                if request.POST.get('version') != locked.updated_at.isoformat():
                    return render(request, 'core/edit_conflict.html', status=409)
            form.save()
        messages.success(request, 'Zapisano zgłoszenie.')
        return redirect(f'core:{route}_list')
    return render(request, 'core/intake_form.html', {'form': form, 'title': title,
        'notified_at': instance.notified_at if instance and model is Recruitment else None,
        'version': request.POST.get('version', '') if request.method == 'POST' else instance.updated_at.isoformat() if instance else ''},
        status=400 if request.method == 'POST' else 200)


@never_cache
@login_required
@require_http_methods(['GET'])
@coordinator_required
def extract_list(request):
    return _list(request, Extract, ExtractForm, 'Ekstrakty', 'extract', ('full_name', 'email', 'phone_number', 'title', 'accepted_titles', 'rejected_titles', 'recruitment'))


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@coordinator_required
def extract_edit(request, pk=None):
    return _edit(request, Extract, ExtractForm, 'Ekstrakt', 'extract', pk)


@never_cache
@login_required
@require_http_methods(['GET'])
@coordinator_required
def recruitment_list(request):
    return _list(request, Recruitment, RecruitmentForm, 'Rekrutacja', 'recruitment', ('first_name', 'last_name', 'email', 'notes', 'unofficial_notes'))


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@coordinator_required
def recruitment_edit(request, pk=None):
    return _edit(request, Recruitment, RecruitmentForm, 'Zgłoszenie rekrutacyjne', 'recruitment', pk)


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
def review_create(request):
    with transaction.atomic():
        form = SingleReviewForm(request.POST if request.method == 'POST' else None)
        if request.method == 'POST' and form.is_valid():
            review = form.save(commit=False)
            apply_blacklist(review)
            review.full_clean()
            review.save()
            messages.success(request, 'Dodano zgłoszenie do recenzji.')
            return redirect('core:assigned_review_detail', review_id=review.pk)
    return render(request, 'core/intake_form.html', {'form': form, 'title': 'Dodaj do recenzji'}, status=400 if request.method == 'POST' else 200)
