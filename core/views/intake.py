from core.public_authors import name_matches
from core.translation_scope import frontend_scope, non_abandoned
from core.author_contact import stored_author_phone
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from core.permissions import coordinator_required, superuser_required
from core.filtering import facet_queryset
from core.pagination import paginate_items
from core.intake_forms import ExtractForm, SingleReviewForm
from core.recruitment_admin_forms import RecruitmentAdminForm
from core.models import Recruitment
from texts.models import Extract, Text
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
        'extract_workflows': list(non_abandoned(Text.objects).filter(import_source='extract-volume-v2').select_related('anthology').order_by('anthology__title')) if model is Extract else [],
        'recruitments': sorted(facets['recruitment'], key=str.casefold) if model is Extract else [],
    })


def _edit(request, model, form_class, title, route, pk):
    instance = get_object_or_404(model, pk=pk) if pk else None
    form = form_class(request.POST if request.method == 'POST' else None, instance=instance)
    if model is Recruitment:
        from core.permissions import can_use_recruitment_mailbox
        if not can_use_recruitment_mailbox(request.user):
            form.fields['notified'].disabled = True
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
    return _list(request, Extract, ExtractForm, 'Ekstrakty', 'extract', ('full_name', 'author__pseudonym', 'email', 'phone_number', 'title', 'accepted_titles', 'rejected_titles', 'recruitment'))


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
    from core.views.recruitment_mailbox import recruitment_register
    return recruitment_register(request)


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@coordinator_required
def recruitment_edit(request, pk=None):
    return _edit(request, Recruitment, RecruitmentAdminForm, 'Zgłoszenie rekrutacyjne', 'recruitment', pk)


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
def review_create(request):
    with transaction.atomic():
        from authors.models import Author
        selected=None
        raw=request.GET.get('author','')
        if raw.isascii() and raw.isdecimal() and len(raw)<19:
            selected=Author.objects.filter(pk=raw).first()
        initial={'author':selected.pk,'author_first_name':selected.display_name if selected.pseudonym.strip() else selected.first_name,'author_last_name':'' if selected.pseudonym.strip() else selected.last_name,'email':selected.email or '', 'phone_number':selected.phone_number or '', 'author_pseudonym':selected.pseudonym} if selected else {}
        form = SingleReviewForm(request.POST if request.method == 'POST' else None,initial=initial)
        if request.method == 'POST' and form.is_valid():
            review = form.save(commit=False)
            from texts.models import Anthology
            # Same lock as bulk imports, including submissions to other calls.
            frontend_scope(Anthology.objects).filter(is_novel=False).select_for_update().order_by('pk').first()
            # Serialize against anthology closure through the actual write.
            anthology = frontend_scope(Anthology.objects).filter(is_novel=False).select_for_update().filter(
                pk=review.anthology_id, status=Anthology.Status.IN_PREPARATION,
            ).first()
            if anthology is None:
                form.add_error('anthology', 'Wybrany nabór nie jest już dostępny. Wybierz antologię w przygotowaniu.')
            else:
                if review.author_id:
                    list(Author.objects.select_for_update().filter(pk=review.author_id))
                # Rebuild the form after locks: cached cleaned_data and signed
                # confirmations must not hide duplicates or a changed blacklist.
                form = SingleReviewForm(request.POST, initial=initial)
                if form.is_valid():
                    review = form.save(commit=False)
                    review.anthology = anthology
                    try:
                        apply_blacklist(review)
                        review.full_clean()
                    except ValidationError as error:
                        form.add_error(None, ' '.join(error.messages))
                    else:
                        review.save()
                        from core.services.newsletters import record_consents
                        record_consents(review.email,
                            premieres=form.cleaned_data['newsletter_premieres'],
                            recruitment=form.cleaned_data['newsletter_recruitment'])
                        messages.success(request, 'Dodano zgłoszenie do recenzji.')
                        return redirect('core:assigned_review_detail', review_id=review.pk)
    fallback_query=request.GET.get('author_query','').strip()[:200]
    fallback_authors=Author.objects.none()
    if fallback_query:
        fallback_authors=Author.objects.all()
        for term in fallback_query.split():
            fallback_authors=fallback_authors.filter(name_matches(term, email=True))
        fallback_authors=fallback_authors.order_by('last_name','first_name','pk')[:30]
    from core.forms import ReviewBulkImportForm
    return render(request, 'core/review_intake.html', {
        'single_form': form, 'bulk_form': ReviewBulkImportForm(user=request.user, auto_id='id_bulk_%s'),
        'author_fallback': True, 'fallback_authors': fallback_authors, 'fallback_query': fallback_query,
    }, status=400 if request.method == 'POST' else 200)


@never_cache
@login_required
@require_http_methods(['GET'])
@superuser_required
def author_suggestions(request):
    from authors.models import Author
    from django.http import JsonResponse
    query = request.GET.get('q', '').strip()[:255]
    authors = Author.objects.all()
    if not query:
        return JsonResponse({'results': []})
    for term in query.split():
        authors = authors.filter(name_matches(term, email=True))
    results = [{'id': author.pk, 'label': author.display_name,
                'first_name': author.display_name if author.pseudonym.strip() else author.first_name, 'last_name': '' if author.pseudonym.strip() else author.last_name,
                'email': author.email or '', 'pseudonym': author.pseudonym, 'phone_number': stored_author_phone(author)} for author in authors.order_by('last_name', 'first_name', 'pk')[:20]]
    response = JsonResponse({'results': results})
    response['Cache-Control'] = 'no-store, private'
    return response
