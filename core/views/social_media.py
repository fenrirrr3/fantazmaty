"""Social Media: plan wpisów promocyjnych o opowiadaniach, lista innych wpisów i cytaty z opowiadań.

Stronę (sekcję „Promocja” w menu) widzą i zmieniają tylko superuser, koordynatorzy i rola „Social Media”.
Lista opowiadań domyślnie obejmuje antologie „Gotowe”; pole „Pokaż teksty niewydane”
dokłada antologie w przygotowaniu. Zawsze pomijane są: antologie porzucone, powieści (rozdziały nie są
osobnymi opowiadaniami), tomy Ekstraktów i teksty wycofane. Każdy wiersz zapisuje się osobno (formularz
per wiersz), z podpisanym tokenem wersji – nieaktualny formularz nie nadpisze cudzej zmiany.
Zakładka Kalendarz (pozycje z datą w widoku miesiąca) – core/views/social_calendar.py.
"""
from collections import defaultdict
from copy import copy

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.db.models import Case, CharField, Exists, F, IntegerField, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponseBadRequest, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from authors.models import Author
from core.account_identity import person_name
from core.edit_versions import version_of
from core.filtering import positive_id
from core.models import EditRevision, SocialMediaEntry, SocialMediaQuote, TextSocialMedia
from core.pagination import paginate_items
from core.permissions import social_media_required
from core.public_authors import name_matches, public_name
from core.social_forms import (SocialMediaEntryForm, SocialMediaQuoteForm, SocialMediaQuoteStatusForm,
                               TextSocialMediaForm)
from core.social_models import SocialMediaStatus
from texts.models import Anthology, ForeignAuthor, Text
from texts.production import active_production_texts

SALT = 'social-media'
UNPUBLISHED = 'unpublished'
# Parametry listy, które wracają po zapisie wiersza (filtry, sortowanie, strona).
# Cytaty mają własne stronicowanie i sortowanie (quotes_*), filtry wyszukiwania i antologii są wspólne.
QUOTE_PARAMS = ('quotes_page', 'quotes_page_size', 'quotes_sort')
LIST_PARAMS = ('q', 'status', 'anthology', UNPUBLISHED, 'sort', 'page', 'page_size', 'filters_applied', *QUOTE_PARAMS)
OPEN_OTHER = 'social_media_open_other'
OPEN_QUOTES = 'social_media_open_quotes'
STATUS_RANK = Case(*[When(social_status=value, then=Value(index)) for index, value in enumerate(SocialMediaStatus.values)],
                   default=Value(0), output_field=IntegerField())
CONFLICT = 'Ktoś zmienił ten wpis w międzyczasie (obecnie: {current}). Sprawdź dane i zapisz ponownie.'


def list_query(raw):
    """Bezpieczny zestaw parametrów listy z ukrytego pola formularza (bez dowolnych kluczy i adresów)."""
    source = QueryDict(str(raw or '')[:2000])
    query = QueryDict(mutable=True)
    for key in LIST_PARAMS:
        values = [value[:200] for value in source.getlist(key)[:20]]
        if values:
            query.setlist(key, values)
    return query


def list_url(query, anchor=''):
    encoded = query.urlencode()
    return reverse('core:social_media') + (f'?{encoded}' if encoded else '') + (f'#{anchor}' if anchor else '')


def story_texts():
    """Wszystkie opowiadania, które mogą trafić do tabeli (bez względu na gotowość antologii)."""
    return active_production_texts(Text.objects.filter(anthology__isnull=False)).exclude(anthology__is_extracts=True)


def _token(user, kind, pk, version):
    return signing.dumps([user.pk, kind, pk, version], salt=SALT)


def _token_matches(token, user, kind, pk, version):
    try:
        return signing.loads(token or '', salt=SALT, max_age=settings.SESSION_COOKIE_AGE) == [user.pk, kind, pk, version]
    except signing.BadSignature:
        return False


def _versions(model, ids):
    label = model._meta.label_lower
    return dict(EditRevision.objects.filter(model_label=label, object_id__in=ids).values_list('object_id', 'version'))


def _values(record):
    """Wartości pól wiersza jako napisy dla formularza."""
    return {
        'status': record.status if record else SocialMediaStatus.FREE,
        'posted_on': record.posted_on.isoformat() if record and record.posted_on else '',
        'url': record.url if record else '',
        'description': getattr(record, 'description', ''),
    }


def _bound_values(form):
    return {name: form.data.get(name, '') for name in ('status', 'posted_on', 'url', 'description')}


def _describe(record):
    if record is None:
        return SocialMediaStatus.FREE.label
    parts = [record.get_status_display()]
    if record.posted_on:
        parts.append(date_format(record.posted_on, 'd.m.Y'))
    if getattr(record, 'url', ''):
        parts.append(record.url)
    return ', '.join(parts)


def _text_queryset(query):
    unpublished = query.get(UNPUBLISHED) == '1'
    scope = story_texts()
    if not unpublished:
        scope = scope.filter(anthology__status=Anthology.Status.READY)
    rows = scope.annotate(social_status=Coalesce(F('social_media__status'), Value(SocialMediaStatus.FREE)))
    q = query.get('q', '').strip()[:200]
    if q:
        local = Author.objects.filter(texts=OuterRef('pk')).filter(name_matches(q))
        foreign = ForeignAuthor.objects.filter(translations__text_id=OuterRef('pk')).filter(name_matches(q))
        # Exists zamiast złączeń M2M: bez duplikatów wierszy i bez distinct().
        rows = rows.alias(_local=Exists(local), _foreign=Exists(foreign)).filter(
            Q(title__plcontains=q) | Q(anthology__title__plcontains=q) | Q(tags__plcontains=q) | Q(genre__plcontains=q)
            | Q(anthology__is_translated=False, _local=True) | Q(anthology__is_translated=True, _foreign=True))
    anthology = query.get('anthology', '')
    if anthology:
        rows = rows.filter(anthology_id=anthology_id) if (anthology_id := positive_id(anthology)) else rows.none()
    local = Author.objects.filter(texts=OuterRef('pk')).annotate(signature=public_name()).order_by('signature')
    foreign = ForeignAuthor.objects.filter(translations__text_id=OuterRef('pk')).annotate(
        signature=public_name()).order_by('signature')
    rows = rows.alias(social_status_rank=STATUS_RANK, social_author=Case(
        When(anthology__is_translated=True, then=Subquery(foreign.values('signature')[:1])),
        default=Subquery(local.values('signature')[:1]), output_field=CharField()))
    rows = rows.select_related('anthology', 'social_media', 'translation').prefetch_related(
        'authors', 'translation__foreign_authors')
    return scope, rows, unpublished


def _status_filter(rows, query):
    """Filtr statusu promocji tekstu – tylko dla Opowiadań (cytaty mają własne statusy)."""
    statuses = [value for value in query.getlist('status') if value]
    if not statuses:
        return rows
    return rows.filter(social_status__in=statuses) if set(statuses) <= set(SocialMediaStatus.values) else rows.none()


def _quote_rows(request, texts, *, quote_form=None, quote_pk=None, add_quote_form=None, add_quote_pk=None):
    """Wiersze sekcji Cytaty: tekst z formularzem nowego cytatu i jego cytaty (pobrane zbiorczo)."""
    quotes = defaultdict(list)
    for quote in SocialMediaQuote.objects.filter(text_id__in=[text.pk for text in texts]).select_related(
            'added_by__person_profile'):
        quotes[quote.text_id].append(quote)
    versions = _versions(SocialMediaQuote, [quote.pk for items in quotes.values() for quote in items])
    rows = []
    for text in texts:
        items = []
        for quote in quotes[text.pk]:
            bound = quote_form is not None and quote.pk == quote_pk
            items.append({
                'quote': quote, 'author': person_name(quote.added_by) or quote.added_by_name,
                'values': {name: quote_form.data.get(name, '') for name in ('status', 'posted_on')} if bound else
                {'status': quote.status, 'posted_on': quote.posted_on.isoformat()},
                'errors': quote_form.errors if bound else {},
                'non_field_errors': quote_form.non_field_errors() if bound else [],
                'token': _token(request.user, 'quote', quote.pk, versions.get(quote.pk, 0)),
            })
        adding = add_quote_form is not None and text.pk == add_quote_pk
        rows.append({
            'pk': text.pk, 'title': text.title, 'anthology': text.anthology, 'authors': text.source_authors_display,
            'quotes': items, 'new_quote': add_quote_form.data.get('content', '') if adding else '',
            'new_quote_errors': add_quote_form.errors.get('content', []) if adding else [],
        })
    return rows


def _manage_context(request, query, list_request, all_rows, *, text_form, text_pk, entry_form, entry_pk, add_form):
    """Sekcje Opowiadania i Inne."""
    page = paginate_items(list_request, _status_filter(all_rows, query))
    texts = list(page.object_list)
    versions = _versions(TextSocialMedia, [text.social_media.pk for text in texts if _record(text)])
    page.object_list = []
    for text in texts:
        record = _record(text)
        bound = text_form is not None and text.pk == text_pk
        page.object_list.append({
            'pk': text.pk, 'title': text.title, 'anthology': text.anthology, 'authors': text.source_authors_display,
            'tags': text.tags, 'genre': text.genre, 'record': record,
            'values': _bound_values(text_form) if bound else _values(record),
            'errors': text_form.errors if bound else {},
            'non_field_errors': text_form.non_field_errors() if bound else [],
            'token': _token(request.user, 'text', text.pk, versions.get(record.pk, 0) if record else 0),
        })
    entries = list(SocialMediaEntry.objects.all())
    entry_versions = _versions(SocialMediaEntry, [entry.pk for entry in entries])
    entry_rows = [{
        'entry': entry,
        'values': _bound_values(entry_form) if entry_form is not None and entry.pk == entry_pk else _values(entry),
        'errors': entry_form.errors if entry_form is not None and entry.pk == entry_pk else {},
        'non_field_errors': entry_form.non_field_errors() if entry_form is not None and entry.pk == entry_pk else [],
        'token': _token(request.user, 'entry', entry.pk, entry_versions.get(entry.pk, 0)),
    } for entry in entries]
    return {
        'page_obj': page, 'rows': page.object_list, 'entry_rows': entry_rows,
        'add_form': add_form or SocialMediaEntryForm(auto_id='social-add-%s'),
        'add_values': _bound_values(add_form) if add_form is not None else _values(None),
        # ?open=other – odnośnik z kalendarza do wpisu w „Inne”.
        'open_other': bool(request.session.pop(OPEN_OTHER, False)) or query.get('open') == 'other' or entry_form is not None or (
            add_form is not None and bool(add_form.errors)),
        'selected_statuses': query.getlist('status'),
    }


def _render(request, query, *, text_form=None, text_pk=None, entry_form=None, entry_pk=None, add_form=None,
            quote_form=None, quote_pk=None, add_quote_form=None, add_quote_pk=None, status=200):
    list_request = copy(request)
    list_request.GET = query
    scope, all_rows, unpublished = _text_queryset(query)
    context = _manage_context(request, query, list_request, all_rows, text_form=text_form, text_pk=text_pk,
                              entry_form=entry_form, entry_pk=entry_pk, add_form=add_form)
    # Cytaty: ten sam zakres i wyszukiwanie co Opowiadania (bez filtra statusu tekstu), osobna paginacja.
    quote_page = paginate_items(list_request, all_rows, page_param='quotes_page', size_param='quotes_page_size',
                                sort_param='quotes_sort', anchor='#social-quotes')
    quote_page.object_list = _quote_rows(request, list(quote_page.object_list), quote_form=quote_form,
                                         quote_pk=quote_pk, add_quote_form=add_quote_form, add_quote_pk=add_quote_pk)
    open_quotes = bool(request.session.pop(OPEN_QUOTES, False)) or quote_form is not None or (
        add_quote_form is not None) or any(key in query for key in QUOTE_PARAMS)
    anthologies = Anthology.objects.filter(pk__in=scope.order_by().values('anthology_id')).only('pk', 'title').order_by('title', 'pk')
    return render(request, 'core/social_media.html', {
        **context, 'return_query': query.urlencode(), 'unpublished': unpublished,
        'quote_page': quote_page, 'quote_rows': quote_page.object_list, 'open_quotes': open_quotes,
        'quote_count': SocialMediaQuote.objects.filter(text__in=all_rows.order_by().values('pk')).count(),
        'quote_max_length': SocialMediaQuote._meta.get_field('content').max_length,
        'query': query.get('q', ''), 'selected_anthology': query.get('anthology', ''), 'anthologies': anthologies,
        'status_choices': SocialMediaStatus.choices,
    }, status=status)


def _record(text):
    try:
        return text.social_media
    except TextSocialMedia.DoesNotExist:
        return None


def _save_text(request, query):
    text_pk = positive_id(request.POST.get('text'))
    if text_pk is None:
        return HttpResponseBadRequest('Nie wskazano tekstu.')
    with transaction.atomic():
        text = get_object_or_404(Text.objects.select_for_update(), pk=text_pk)
        if not story_texts().filter(pk=text.pk).exists():
            raise Http404('Tego tekstu nie ma w tabeli Social Media.')
        record = TextSocialMedia.objects.select_for_update().filter(text=text).first()
        current = _describe(record)  # przed walidacją – formularz nadpisuje pola instancji
        form = TextSocialMediaForm(request.POST, instance=record or TextSocialMedia(text=text))
        if not _token_matches(request.POST.get('version'), request.user, 'text', text.pk, version_of(record) if record else 0):
            form.is_valid()
            form.add_error(None, CONFLICT.format(current=current))
            messages.error(request, f'Nie zapisano „{text.title}” – ktoś zmienił ten wpis w międzyczasie.')
            return _render(request, query, text_form=form, text_pk=text.pk, status=409)
        if form.is_valid():
            saved = form.save(commit=False)
            saved.updated_by = request.user
            saved.save()
            messages.success(request, f'Zapisano „{text.title}”.')
            return redirect(list_url(query, f'social-text-{text.pk}'))
    messages.error(request, f'Nie zapisano „{text.title}” – popraw zaznaczone pola.')
    return _render(request, query, text_form=form, text_pk=text.pk, status=400)


def _save_entry(request, query, action):
    if action == 'entry_add':
        form = SocialMediaEntryForm(request.POST, auto_id='social-add-%s')
        if not form.is_valid():
            messages.error(request, 'Nie dodano wpisu – popraw zaznaczone pola.')
            return _render(request, query, add_form=form, status=400)
        entry = form.save(commit=False)
        entry.updated_by = request.user
        entry.save()
        messages.success(request, 'Dodano wpis.')
        request.session[OPEN_OTHER] = True
        return redirect(list_url(query, f'social-entry-{entry.pk}'))
    entry_pk = positive_id(request.POST.get('entry'))
    if entry_pk is None:
        return HttpResponseBadRequest('Nie wskazano wpisu.')
    with transaction.atomic():
        entry = SocialMediaEntry.objects.select_for_update().filter(pk=entry_pk).first()
        if entry is None:
            raise Http404('Wpis nie istnieje.')
        if not _token_matches(request.POST.get('version'), request.user, 'entry', entry.pk, version_of(entry)):
            if action == 'entry_delete':
                messages.error(request, f'Nie usunięto wpisu „{entry.description}” – ktoś zmienił go w międzyczasie.')
                request.session[OPEN_OTHER] = True
                return redirect(list_url(query, f'social-entry-{entry.pk}'))
            current = f'{entry.description}, {_describe(entry)}'
            form = SocialMediaEntryForm(request.POST, instance=entry)
            form.is_valid()
            form.add_error(None, CONFLICT.format(current=current))
            messages.error(request, 'Nie zapisano wpisu – ktoś zmienił go w międzyczasie.')
            return _render(request, query, entry_form=form, entry_pk=entry.pk, status=409)
        if action == 'entry_delete':
            entry.delete()
            messages.success(request, f'Usunięto wpis „{entry.description}”.')
            request.session[OPEN_OTHER] = True
            return redirect(list_url(query, 'social-other'))
        form = SocialMediaEntryForm(request.POST, instance=entry)
        if form.is_valid():
            saved = form.save(commit=False)
            saved.updated_by = request.user
            saved.save()
            messages.success(request, 'Zapisano wpis.')
            request.session[OPEN_OTHER] = True
            return redirect(list_url(query, f'social-entry-{entry.pk}'))
    messages.error(request, 'Nie zapisano wpisu – popraw zaznaczone pola.')
    return _render(request, query, entry_form=form, entry_pk=entry_pk, status=400)


def _add_quote(request, query):
    text_pk = positive_id(request.POST.get('text'))
    if text_pk is None:
        return HttpResponseBadRequest('Nie wskazano tekstu.')
    text = story_texts().filter(pk=text_pk).first()
    if text is None:
        raise Http404('Tego tekstu nie ma w tabeli Social Media.')
    form = SocialMediaQuoteForm(request.POST)
    if not form.is_valid():
        messages.error(request, f'Nie zapisano cytatu do „{text.title}” – popraw treść.')
        return _render(request, query, add_quote_form=form, add_quote_pk=text.pk, status=400)
    quote = form.save(commit=False)
    quote.text, quote.posted_on = text, timezone.localdate()
    quote.added_by = quote.updated_by = request.user
    quote.added_by_name = person_name(request.user)[:200]
    quote.save()
    messages.success(request, f'Zapisano cytat do „{text.title}”.')
    request.session[OPEN_QUOTES] = True
    return redirect(list_url(query, f'social-quote-text-{text.pk}'))


def _change_quote(request, query, action):
    """Status, data i usunięcie cytatu (stronę widzą tylko osoby od Social Media)."""
    quote_pk = positive_id(request.POST.get('quote'))
    if quote_pk is None:
        return HttpResponseBadRequest('Nie wskazano cytatu.')
    with transaction.atomic():
        quote = SocialMediaQuote.objects.select_for_update().select_related('text').filter(pk=quote_pk).first()
        if quote is None:
            raise Http404('Cytat nie istnieje.')
        title, anchor = quote.text.title, f'social-quote-text-{quote.text_id}'
        if not _token_matches(request.POST.get('version'), request.user, 'quote', quote.pk, version_of(quote)):
            if action == 'quote_delete':
                messages.error(request, f'Nie usunięto cytatu do „{title}” – ktoś zmienił go w międzyczasie.')
                request.session[OPEN_QUOTES] = True
                return redirect(list_url(query, anchor))
            current = _describe(quote)
            form = SocialMediaQuoteStatusForm(request.POST, instance=quote)
            form.is_valid()
            form.add_error(None, CONFLICT.format(current=current))
            messages.error(request, f'Nie zapisano cytatu do „{title}” – ktoś zmienił go w międzyczasie.')
            return _render(request, query, quote_form=form, quote_pk=quote.pk, status=409)
        if action == 'quote_delete':
            quote.delete()
            messages.success(request, f'Usunięto cytat do „{title}”.')
            request.session[OPEN_QUOTES] = True
            return redirect(list_url(query, anchor))
        form = SocialMediaQuoteStatusForm(request.POST, instance=quote)
        if form.is_valid():
            saved = form.save(commit=False)
            saved.updated_by = request.user
            saved.save()
            messages.success(request, f'Zapisano cytat do „{title}”.')
            request.session[OPEN_QUOTES] = True
            return redirect(list_url(query, anchor))
    messages.error(request, f'Nie zapisano cytatu do „{title}” – popraw zaznaczone pola.')
    return _render(request, query, quote_form=form, quote_pk=quote_pk, status=400)


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@social_media_required
def social_media(request):
    if request.method == 'GET':
        return _render(request, request.GET)
    query = list_query(request.POST.get('return_query'))
    action = request.POST.get('action')
    if action == 'quote_add':
        return _add_quote(request, query)
    if action in ('quote_edit', 'quote_delete'):
        return _change_quote(request, query, action)
    if action not in ('text', 'entry_add', 'entry_edit', 'entry_delete'):
        return HttpResponseBadRequest('Nieznana operacja.')
    if action == 'text':
        return _save_text(request, query)
    return _save_entry(request, query, action)
