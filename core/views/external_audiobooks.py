"""Public, read-only catalogue with a fixed projection of publication data."""
from django.core.exceptions import ValidationError
from django.db.models import Case, CharField, Q, Value, When
from django.db.models.functions import Coalesce
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.audiobook_validators import validate_mega_url
from core.models import Audiobook, PublicAudiobookSettings
from core.pagination import paginate_items
from core.public_guidelines import PublicGuidelines, external_page_headers
from texts.models import Anthology, Text
from texts.production import active_production_texts


def visible_audiobooks(*, published=False):
    """Teksty gotowych antologii w zakresie produkcji audiobooków (zgoda na nagranie, poza czarną listą,
    niewycofane): domyślnie bez opublikowanych audiobooków, z ``published`` – tylko opublikowane."""
    published_audio = Q(audiobook__status=Audiobook.Status.PUBLISHED)
    return active_production_texts(Text.objects.filter(
        for_recording=True, audiobook_blacklisted=False,
        anthology__status=Anthology.Status.READY,
    ).filter(published_audio if published else Q(audiobook__isnull=True) | ~published_audio))


GUIDELINES = PublicGuidelines('Wytyczne do nagrywania', 'wytyczne-audiobooki.pdf', 'core:audiobook_guidelines_pdf')


def public_config():
    config = PublicAudiobookSettings.objects.filter(pk=1).values('mega_url', 'guidelines').first()
    config = config or {'mega_url': 'https://mega.nz/', 'guidelines': ''}
    try:
        validate_mega_url(config['mega_url'])
    except ValidationError:
        config['mega_url'] = ''
    return config


@never_cache
@require_GET
def external_audiobooks(request):
    status = request.GET.get('status', '')
    published = status == Audiobook.Status.PUBLISHED
    rows = visible_audiobooks(published=published)
    choices = rows.order_by('anthology__title').values('anthology_id', 'anthology__title').distinct()
    anthology = request.GET.get('anthology', '')
    if anthology:
        rows = rows.filter(anthology_id=int(anthology)) if anthology.isascii() and anthology.isdecimal() and len(anthology) <= 18 else rows.none()
    query = request.GET.get('q', '').strip()[:200]
    if query:
        rows = rows.filter(Q(title__plcontains=query) | Q(tags__plcontains=query)
                           | Q(genre__plcontains=query) | Q(anthology__title__plcontains=query)
                           | Q(audiobook__narrator_name__plcontains=query))
    rows = rows.annotate(audio_status=Coalesce('audiobook__status', Value(Audiobook.Status.PENDING)))
    if status:
        rows = rows.filter(audio_status=status) if status in Audiobook.Status.values else rows.none()
    # Narrator names help people check their own assignments. E-mails, file URLs
    # and private notes stay out of the public context.
    rows = rows.annotate(public_status=Case(*[When(audio_status=value, then=Value(label)) for value, label in Audiobook.Status.choices],
        default=Value('Do nagrania'), output_field=CharField())).values(
        'anthology__title', 'title', 'tags', 'genre', 'public_status', 'audio_status', 'audiobook__narrator_name',
    )
    page = paginate_items(request, rows)
    config = public_config()
    return external_page_headers(render(request, 'core/external_audiobooks.html', {
        'rows': page, 'page_obj': page, 'anthologies': choices,
        'selected_anthology': anthology, 'selected_status': status, 'query': query, 'published': published,
        'status_choices': Audiobook.Status.choices,
        'mega_url': config['mega_url'], 'guidelines': GUIDELINES.context(config['guidelines']),
    }))


@never_cache
@require_GET
def audiobook_guidelines_pdf(request):
    text = PublicAudiobookSettings.objects.filter(pk=1).values_list('guidelines', flat=True).first()
    return GUIDELINES.pdf_response(text or '')
