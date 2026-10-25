from core.filtering import positive_id
from core.public_authors import name_matches
from core.translation_scope import frontend_scope, non_abandoned
from core.author_contact import stored_author_phone
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.core.exceptions import ValidationError
from django.db.models import Q
from copy import copy
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
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
    query = request.GET.get('q', '').strip()[:500]
    statuses = [v for v in request.GET.getlist('status') if v in Extract.Status.values]
    items = Extract.objects.select_related('author')
    for term in query.split():
        condition = Q()
        for field in ('full_name', 'author__pseudonym', 'email', 'phone_number', 'title', 'accepted_titles', 'rejected_titles', 'recruitment'):
            condition |= Q(**{f'{field}__plcontains': term})
        items = items.filter(condition)
    recruitment = request.GET.get('recruitment', '').strip()
    items, facets = facet_queryset(items, {
        'status': ('status', statuses),
        'recruitment': ('recruitment', [recruitment] if recruitment else []),
    })
    page = paginate_items(request, items)
    return render(request, 'core/extract_list.html', {
        'title': 'Ekstrakty', 'route': 'extract', 'items': page, 'page_obj': page,
        'query': query, 'status_choices': [(v, label) for v, label in Extract.Status.choices if v in facets['status']], 'selected_statuses': statuses,
        'recruitment': recruitment,
        'extract_workflows': list(non_abandoned(Text.objects).filter(import_source='extract-volume-v2').select_related('anthology').order_by('anthology__title')),
        'recruitments': sorted(facets['recruitment'], key=str.casefold),
    })


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


def _author_initial(request):
    """Dane autora wybranego parametrem ?author= (formularz pojedynczego zgłoszenia)."""
    from authors.models import Author
    author_id = positive_id(request.GET.get('author'))
    selected = Author.objects.filter(pk=author_id).first() if author_id else None
    if selected is None:
        return {}
    pseudonym = selected.pseudonym.strip()
    return {'author': selected.pk, 'author_first_name': selected.display_name if pseudonym else selected.first_name,
            'author_last_name': '' if pseudonym else selected.last_name, 'email': selected.email or '',
            'phone_number': selected.phone_number or '', 'author_pseudonym': selected.pseudonym}


def manual_intake(request, **overrides):
    """Kontekst zwiniętej sekcji „Dodaj ręcznie” na stronie „Dodaj do recenzji”."""
    from authors.models import Author
    from core.forms import ReviewBulkImportForm
    fallback_query = request.GET.get('author_query', '').strip()[:200]
    fallback_authors = Author.objects.none()
    if fallback_query:
        fallback_authors = Author.objects.all()
        for term in fallback_query.split():
            fallback_authors = fallback_authors.filter(name_matches(term, email=True))
        fallback_authors = fallback_authors.order_by('last_name', 'first_name', 'pk')[:30]
    context = {'single_form': SingleReviewForm(initial=_author_initial(request)),
               'bulk_form': ReviewBulkImportForm(user=request.user, auto_id='id_bulk_%s'),
               'fallback_authors': fallback_authors, 'fallback_query': fallback_query}
    context.update(overrides)
    return context


def render_intake_page(request, status, **manual):
    """Strona „Dodaj do recenzji” po wysłaniu formularza ręcznego.

    Część skrzynki powstaje jak przy GET, a sekcja ręczna jest rozwinięta
    i pokazuje przekazany formularz z błędami lub podglądem.
    """
    from core.views.mailbox import mailbox_headers
    page = copy(request)
    page.method, page.POST = 'GET', QueryDict()
    page.manual_intake = manual_intake(request, open=True, **manual)
    response = mailbox_headers(page)
    response.status_code = status
    return response


def intake_page_redirect(request):
    """Dawne adresy ręcznego dodawania prowadzą do scalonej strony (z parametrami wyboru autora)."""
    query = request.GET.urlencode()
    return redirect(reverse('core:review_bulk_import') + (f'?{query}' if query else ''))


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
def review_create(request):
    if request.method == 'GET':
        return intake_page_redirect(request)
    from authors.models import Author
    with transaction.atomic():
        form = SingleReviewForm(request.POST)
        if form.is_valid():
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
                form = SingleReviewForm(request.POST)
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
    return render_intake_page(request, 400, single_form=form)


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
