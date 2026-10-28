"""Kalendarz Social Media: plan wpisów na kolejne dni w widoku miesiąca.

Kalendarz pokazuje wpisy dodane w kalendarzu („kto i co wrzuca danego dnia” – informacyjnie, bez blokowania dnia)
oraz pozycje z listy Social Media, które mają datę: opowiadania i „Inne” z datą wrzucenia (tylko dla osób od
Social Media – jak na liście) i cytaty w przygotowaniu albo wrzucone (cały zespół). Cytat „Wolne” ma jako datę
tylko dzień dodania, więc w kalendarzu go nie ma.

Stronę widzi cały zespół. Wpisy dodaje superuser, koordynator i rola „Social Media”; zmienia i usuwa autor wpisu
(dopóki ma te uprawnienia) albo koordynator. Zapisy idą zwykłym formularzem (POST i przekierowanie), z podpisanym
tokenem wersji – nieaktualny formularz nie nadpisze cudzej zmiany. Nowy wpis wysyła powiadomienie Discord
(reguła „Nowy wpis w kalendarzu Social Media”) – po zatwierdzeniu transakcji; edycja – nie.
"""
import datetime
import re
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponseBadRequest, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dates import WEEKDAYS, WEEKDAYS_ABBR
from django.utils.formats import date_format
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.account_identity import person_name
from core.discord_notifications import social_calendar_added
from core.edit_versions import version_of
from core.filtering import positive_id
from core.inflection import count_text
from core.permissions import is_coordinator, social_media_required
from core.social_forms import SocialCalendarEntryForm
from core.social_models import SocialCalendarEntry, SocialMediaEntry, SocialMediaQuote, SocialMediaStatus, TextSocialMedia
from core.views.social_media import CONFLICT, _token, _token_matches, _versions, list_url, story_texts

MONTH_RE = re.compile(r'^(\d{4})-(\d{1,2})$')
FIRST_YEAR, LAST_YEAR = 2000, 2100
ITEM_FORMS = ('wpis', 'wpisy', 'wpisów')
# Rodzaje pozycji w komórce dnia (kolejność w legendzie i w komórce).
KINDS = {'calendar': 'Plan', 'text': 'Opowiadanie', 'entry': 'Inne', 'quote': 'Cytat'}
# Cytat „Wolne” ma datę dodania, nie planowaną – nie trafia do kalendarza.
DATED_QUOTE_STATUSES = (SocialMediaStatus.IN_PROGRESS, SocialMediaStatus.POSTED)


def month_start(raw, today):
    """Pierwszy dzień miesiąca z parametru „RRRR-MM”; brak, błąd albo rok spoza 2000–2100 – bieżący miesiąc."""
    match = MONTH_RE.match(str(raw or '').strip())
    if match:
        year, month = int(match[1]), int(match[2])
        if FIRST_YEAR <= year <= LAST_YEAR and 1 <= month <= 12:
            return datetime.date(year, month, 1)
    return today.replace(day=1)


def parse_day(raw):
    """Dzień z parametru „RRRR-MM-DD” (wybór dnia w kalendarzu); błąd – None."""
    try:
        day = datetime.date.fromisoformat(str(raw or '').strip()[:10])
    except ValueError:
        return None
    return day if FIRST_YEAR <= day.year <= LAST_YEAR else None


def shift_month(first, months):
    index = first.year * 12 + first.month - 1 + months
    return datetime.date(index // 12, index % 12 + 1, 1)


def calendar_url(month=None, day=None, anchor=None, **params):
    """Adres kalendarza: miesiąc (RRRR-MM), wybrany dzień i kotwica (domyślnie – komórka dnia)."""
    query = QueryDict(mutable=True)
    if month is not None:
        query['month'] = f'{month:%Y-%m}'
    if day is not None:
        query['day'] = day.isoformat()
    for key, value in params.items():
        query[key] = value
    if anchor is None and day is not None:
        anchor = f'social-day-{day:%Y-%m-%d}'
    encoded = query.urlencode()
    return reverse('core:social_calendar') + (f'?{encoded}' if encoded else '') + (f'#{anchor}' if anchor else '')


def can_change(user, entry):
    """Zmiana i usunięcie wpisu: koordynator (także superuser) albo autor wpisu."""
    return is_coordinator(user) or entry.added_by_id == user.pk


def day_label(day):
    return date_format(day, 'j E Y')


def _list_link(**params):
    query = QueryDict(mutable=True)
    anchor = params.pop('anchor')
    for key, value in params.items():
        query[key] = value
    return list_url(query, anchor)


def dated_items(user, start, end):
    """{dzień: [pozycje]} z kalendarza i listy Social Media w przedziale dni (wpisy kalendarza najpierw)."""
    items = defaultdict(list)
    entries = list(SocialCalendarEntry.objects.filter(day__range=(start, end)).select_related('added_by__person_profile'))
    versions = _versions(SocialCalendarEntry, [entry.pk for entry in entries])
    for entry in entries:
        changeable = can_change(user, entry)
        items[entry.day].append({
            'kind': 'calendar', 'kind_label': KINDS['calendar'], 'entry': entry, 'title': entry.description,
            'detail': entry.get_platform_display(), 'author': person_name(entry.added_by) or entry.added_by_name,
            'can_change': changeable, 'edit_url': calendar_url(entry.day.replace(day=1), edit=entry.pk,
                                                               anchor='social-calendar-form') if changeable else '',
            'token': _token(user, 'calendar', entry.pk, versions.get(entry.pk, 0)) if changeable else '',
        })
    stories = story_texts().order_by().values('pk')
    for record in TextSocialMedia.objects.filter(posted_on__range=(start, end), text__in=stories).select_related('text'):
        items[record.posted_on].append({
            'kind': 'text', 'kind_label': KINDS['text'], 'title': f'„{record.text.title}”',
            'status': record.status, 'detail': record.get_status_display(),
            'url': _list_link(q=record.text.title, unpublished='1', anchor=f'social-text-{record.text_id}'),
        })
    for entry in SocialMediaEntry.objects.filter(posted_on__range=(start, end)).order_by('pk'):
        items[entry.posted_on].append({
            'kind': 'entry', 'kind_label': KINDS['entry'], 'title': entry.description, 'status': entry.status,
            'detail': entry.get_status_display(), 'url': _list_link(open='other', anchor=f'social-entry-{entry.pk}'),
        })
    quotes = SocialMediaQuote.objects.filter(posted_on__range=(start, end), status__in=DATED_QUOTE_STATUSES,
                                             text__in=stories).select_related('text').order_by('created_at', 'pk')
    for quote in quotes:
        items[quote.posted_on].append({
            'kind': 'quote', 'kind_label': KINDS['quote'], 'title': f'Cytat z „{quote.text.title}”',
            'status': quote.status, 'detail': quote.get_status_display(), 'quote': quote.content,
            'url': _list_link(q=quote.text.title, unpublished='1', quotes_page='1',
                              anchor=f'social-quote-text-{quote.text_id}'),
        })
    return items


def grid_range(first):
    """Pierwszy i ostatni dzień siatki miesiąca: od poniedziałku do niedzieli (z dniami sąsiednich miesięcy)."""
    last = shift_month(first, 1) - datetime.timedelta(days=1)
    return first - datetime.timedelta(days=first.weekday()), last + datetime.timedelta(days=6 - last.weekday())


def month_weeks(first, today, items):
    """Tygodnie siatki miesiąca – listy po siedem dni z pozycjami."""
    start, end = grid_range(first)
    days = []
    for offset in range((end - start).days + 1):
        day = start + datetime.timedelta(days=offset)
        day_items = items.get(day, [])
        days.append({
            'date': day, 'iso': day.isoformat(), 'in_month': day.month == first.month, 'is_today': day == today,
            'is_past': day < today, 'weekday': str(WEEKDAYS[day.weekday()]).lower(), 'label': day_label(day),
            'month_name': date_format(day, 'E'),
            'items': day_items, 'count_text': count_text(len(day_items), ITEM_FORMS) if day_items else '',
            'add_url': calendar_url(day.replace(day=1), day=day, anchor='social-calendar-form'),
        })
    return [days[index:index + 7] for index in range(0, len(days), 7)]


def _render(request, *, first=None, form=None, edit_entry=None, edit_token='', selected_day=None, status=200):
    today = timezone.localdate()
    first = first or month_start(request.GET.get('month'), today)
    grid_start, grid_end = grid_range(first)
    items = dated_items(request.user, grid_start, grid_end)
    weeks = month_weeks(first, today, items)
    if form is None:
        form = SocialCalendarEntryForm(initial={'day': selected_day or (today if first.month == today.month and
                                                                        first.year == today.year else first)})
    form_day = selected_day
    if form is not None and form.is_bound:
        form_day = parse_day(form.data.get('day'))
    busy = []
    if form_day is not None:
        busy = [item for item in (items.get(form_day) if grid_start <= form_day <= grid_end
                                  else dated_items(request.user, form_day, form_day).get(form_day)) or []
                if edit_entry is None or item.get('entry') != edit_entry]
    month_count = sum(len(day['items']) for week in weeks for day in week if day['in_month'])
    return render(request, 'core/social_calendar.html', {
        'weeks': weeks, 'weekdays': [str(name) for name in WEEKDAYS_ABBR.values()],
        'month_title': date_format(first, 'F Y'), 'month_value': f'{first:%Y-%m}', 'month_count': month_count,
        'month_count_text': count_text(month_count, ITEM_FORMS),
        'prev_url': calendar_url(shift_month(first, -1)), 'next_url': calendar_url(shift_month(first, 1)),
        'today_url': calendar_url(anchor=f'social-day-{today:%Y-%m-%d}'), 'is_current_month': first == today.replace(day=1),
        'form': form, 'edit_entry': edit_entry, 'edit_token': edit_token, 'kinds': KINDS,
        'busy_day': form_day.isoformat() if form_day else '', 'busy_day_label': day_label(form_day) if form_day else '', 'busy_items': busy,
        'description_max_length': SocialCalendarEntry._meta.get_field('description').max_length,
        'cancel_url': calendar_url(first, anchor=None),
    }, status=status)


def _edit_get(request, first):
    """?edit=<id> – formularz zmiany wpisu (tylko dla osoby, która może go zmienić; inaczej zwykły widok)."""
    entry_pk = positive_id(request.GET.get('edit'))
    entry = SocialCalendarEntry.objects.filter(pk=entry_pk).first() if entry_pk else None
    if entry is None or not can_change(request.user, entry):
        return None
    form = SocialCalendarEntryForm(instance=entry)
    return _render(request, first=first, form=form, edit_entry=entry, selected_day=entry.day,
                   edit_token=_token(request.user, 'calendar', entry.pk, version_of(entry)))


def _add(request):
    form = SocialCalendarEntryForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Nie dodano wpisu – popraw zaznaczone pola.')
        return _render(request, first=_return_month(request), form=form, status=400)
    with transaction.atomic():
        entry = form.save(commit=False)
        entry.added_by = entry.updated_by = request.user
        entry.added_by_name = person_name(request.user)[:200]
        entry.save()
        # Powiadomienie trafia do kolejki w tej transakcji, a wysyłka – po jej zatwierdzeniu.
        social_calendar_added(entry)
    others = [item for item in dated_items(request.user, entry.day, entry.day).get(entry.day, [])
              if item.get('entry') != entry]
    messages.success(request, f'Dodano wpis na {day_label(entry.day)}.')
    if others:
        messages.warning(request, f'Na {day_label(entry.day)} zaplanowano już {count_text(len(others), ITEM_FORMS)} – '
                                  'sprawdź w kalendarzu, czy wpisy sobie nie przeszkadzają.')
    return redirect(calendar_url(entry.day.replace(day=1), anchor=f'social-day-{entry.day:%Y-%m-%d}'))


def _change(request, action):
    entry_pk = positive_id(request.POST.get('entry'))
    if entry_pk is None:
        return HttpResponseBadRequest('Nie wskazano wpisu.')
    with transaction.atomic():
        entry = SocialCalendarEntry.objects.select_for_update().filter(pk=entry_pk).first()
        if entry is None:
            raise Http404('Wpis nie istnieje.')
        if not can_change(request.user, entry):
            raise PermissionDenied('Wpis w kalendarzu zmienia i usuwa jego autor albo koordynator.')
        back = calendar_url(entry.day.replace(day=1), anchor=f'social-day-{entry.day:%Y-%m-%d}')
        if not _token_matches(request.POST.get('version'), request.user, 'calendar', entry.pk, version_of(entry)):
            if action == 'delete':
                messages.error(request, f'Nie usunięto wpisu „{entry.description}” – ktoś zmienił go w międzyczasie.')
                return redirect(back)
            current = f'{day_label(entry.day)}, {entry.get_platform_display()}: {entry.description}'
            form = SocialCalendarEntryForm(request.POST, instance=entry)
            form.is_valid()
            form.add_error(None, CONFLICT.format(current=current))
            messages.error(request, 'Nie zapisano wpisu – ktoś zmienił go w międzyczasie.')
            return _render(request, first=_return_month(request), form=form, edit_entry=entry, status=409,
                           edit_token=_token(request.user, 'calendar', entry.pk, version_of(entry)))
        if action == 'delete':
            entry.delete()
            messages.success(request, f'Usunięto wpis „{entry.description}”.')
            return redirect(back)
        token = request.POST.get('version')
        form = SocialCalendarEntryForm(request.POST, instance=entry)
        if form.is_valid():
            saved = form.save(commit=False)
            saved.updated_by = request.user
            saved.save()
            messages.success(request, f'Zapisano wpis na {day_label(saved.day)}.')
            return redirect(calendar_url(saved.day.replace(day=1), anchor=f'social-day-{saved.day:%Y-%m-%d}'))
    messages.error(request, 'Nie zapisano wpisu – popraw zaznaczone pola.')
    entry.refresh_from_db()
    return _render(request, first=_return_month(request), form=form, edit_entry=entry, edit_token=token, status=400)


def _return_month(request):
    return month_start(request.POST.get('month'), timezone.localdate())


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@social_media_required
def social_calendar(request):
    if request.method == 'GET':
        today = timezone.localdate()
        selected = parse_day(request.GET.get('day'))
        first = month_start(request.GET.get('month'), today) if 'month' in request.GET or selected is None else (
            selected.replace(day=1))
        if 'edit' in request.GET:
            response = _edit_get(request, first)
            if response is not None:
                return response
        return _render(request, first=first, selected_day=selected)
    action = request.POST.get('action')
    if action not in ('add', 'edit', 'delete'):
        return HttpResponseBadRequest('Nieznana operacja.')
    if action == 'add':
        return _add(request)
    return _change(request, action)
