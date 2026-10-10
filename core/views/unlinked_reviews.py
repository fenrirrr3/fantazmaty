"""Superuser-only audit of texts without an explicit source submission."""
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from core.pagination import paginate_items
from core.selectors.texts import _ProjectedRows
from core.permissions import superuser_required
from core.public_authors import name_matches
from texts.models import Anthology, Text


@never_cache
@login_required
@superuser_required
@require_GET
def unlinked_reviews(request):
    texts = Text.objects.filter(anthology__isnull=False, source_review__isnull=True).exclude(
        anthology__status=Anthology.Status.READY).select_related('anthology').prefetch_related('authors')
    show_not_applicable = request.GET.get('show_not_applicable') == '1'
    if not show_not_applicable:
        texts = texts.filter(chapter_number__isnull=True).exclude(import_source='extract-volume-v2')
    books = Anthology.objects.filter(pk__in=texts.values('anthology_id')).order_by('title', 'pk')
    query = request.GET.get('q', '').strip()[:200]
    selected = request.GET.get('anthology', '')
    if selected.isascii() and selected.isdecimal() and len(selected) <= 18:
        texts = texts.filter(anthology_id=int(selected))
    if query:
        texts = texts.filter(Q(title__plcontains=query) | Q(anthology__title__plcontains=query)
                            | name_matches(query, prefix='authors__')).distinct()
    page = paginate_items(request, _ProjectedRows(texts.order_by('anthology__title', 'title', 'pk'),
        lambda text: {'pk': text.pk, 'title': text.title, 'anthology': text.anthology,
                      'authors_display': text.authors_display,
                      'not_applicable': bool(text.chapter_number is not None or text.import_source == 'extract-volume-v2')}))
    return render(request, 'core/unlinked_reviews.html', {
        'texts': page, 'page_obj': page, 'query': query, 'anthologies': books,
        'selected_anthology': selected, 'show_not_applicable': show_not_applicable,
    })
