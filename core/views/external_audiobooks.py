"""Public, read-only catalogue with a fixed projection of publication data."""
from django.core.exceptions import ValidationError
from django.db.models import Q, Value, CharField
from django.http import HttpResponse, Http404
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.audiobook_validators import validate_mega_url
from core.models import PublicAudiobookSettings
from core.pagination import paginate_items
from texts.models import Anthology, Text
from texts.production import active_production_texts


def visible_audiobooks():
    return active_production_texts(Text.objects.filter(
        for_recording=True, audiobook_blacklisted=False,
        anthology__status=Anthology.Status.READY,
    ).filter(Q(audiobook__isnull=True) | Q(audiobook__status='pending')))


def public_config():
    config = PublicAudiobookSettings.objects.filter(pk=1).values('mega_url', 'guidelines').first()
    config = config or {'mega_url': 'https://mega.nz/', 'guidelines': ''}
    if not config['guidelines'].strip():
        config['guidelines'] = ''
    try:
        validate_mega_url(config['mega_url'])
    except ValidationError:
        config['mega_url'] = ''
    return config


@never_cache
@require_GET
def external_audiobooks(request):
    rows = visible_audiobooks()
    choices = rows.order_by('anthology__title').values('anthology_id', 'anthology__title').distinct()
    anthology = request.GET.get('anthology', '')
    if anthology:
        rows = rows.filter(anthology_id=int(anthology)) if anthology.isascii() and anthology.isdecimal() and len(anthology) <= 18 else rows.none()
    query = request.GET.get('q', '').strip()[:200]
    if query:
        rows = rows.filter(Q(title__plcontains=query) | Q(tags__plcontains=query)
                           | Q(genre__plcontains=query) | Q(anthology__title__plcontains=query))
    status = request.GET.get('status', '')
    if status and status != 'Do nagrania':
        rows = rows.none()
    # Keep contact details, file URLs and private notes out of the public context.
    rows = rows.annotate(public_status=Value('Do nagrania', output_field=CharField())).values(
        'anthology__title', 'title', 'tags', 'genre', 'public_status',
    )
    page = paginate_items(request, rows)
    response = render(request, 'core/external_audiobooks.html', {
        'rows': page, 'page_obj': page, 'anthologies': choices,
        'selected_anthology': anthology, 'selected_status': status, 'query': query,
        **public_config(),
    })
    response['X-Robots-Tag'] = 'noindex, nofollow'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@never_cache
@require_GET
def audiobook_guidelines_txt(request):
    content = public_config()['guidelines']
    if not content.strip():
        raise Http404('Wytyczne nie zostały jeszcze uzupełnione.')
    response = HttpResponse(content, content_type='text/plain; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="wytyczne-audiobooki.txt"'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    response['Referrer-Policy'] = 'no-referrer'
    return response
