"""Słuchaj WWW: lista audiobooków dla strony fantazmaty.pl/sluchaj/.

Ten sam słownik dla endpointu i podglądu w zakładce. Nagrania to audiobooki CMS-u o statusie „Opublikowane” z ustawioną
datą premiery i co najmniej jednym linkiem (YouTube albo HearThis); każda część (``Audiobook.additional_links``) to osobna karta. Antologię, okładkę i kolejność daje
``SiteListenAnthology``, czasy nagrań i stały identyfikator karty – ``SiteListenRecording`` (CMS nie zna czasów).
Tytuł i autor nagrania są brane z tekstu CMS-u, a nazwa antologii – z ``Anthology.title`` (dla wpisu powiązanego),
więc zmiana w CMS-ie trafia do JSON-a od razu.
Twórcy: Lektor – ``Audiobook.narrator_name``, Montaż – ``Audiobook.engineer_name``, Korekta – wykonawcy etapów
„Korekta” (``AudiobookStage.performer``), a bez nich bieżący ``Audiobook.proofreader``. Do JSON-a trafiają wyłącznie
tytuły, podpisy autorów, imiona i nazwiska twórców, adresy nagrań i okładek – nigdy e-maile ani loginy.
"""
import re
from urllib.parse import parse_qs, urlsplit

from django.db.models import Prefetch
from django.utils import timezone
from django.utils.text import slugify

from core.audiobook_models import Audiobook, AudiobookStage, StageType
from core.discord_notifications import text_authors
from core.odkurzacz_forms import plural
from core.site_catalog import anthology_key, fold
from core.site_models import SiteListenAnthology, SiteListenRecording, SiteTeamSettings, with_version
from texts.models import Anthology

PART_SUFFIX_RE = re.compile(r',?\s*cz\.\s*(\d+)\s*$', re.IGNORECASE)
RECORDING_FORMS = ('nagranie', 'nagrania', 'nagrań')
CREDIT_KEYS = ('lektor', 'korekta', 'montaz')


# --- adresy i części ------------------------------------------------------------------------------------------------

def media_key(url):
    """Klucz porównania adresu nagrania: bez schematu, ``www.``/``m.``, końcowego ``/`` i zbędnych parametrów;
    ``youtu.be/ID`` i ``youtube.com/watch?v=ID`` dają ten sam klucz. Pusty adres – pusty klucz."""
    url = (url or '').strip()
    if not url:
        return ''
    try:
        parsed = urlsplit(url if '//' in url else f'https://{url}')
    except ValueError:
        return ''
    host = (parsed.hostname or '').lower()
    for prefix in ('www.', 'm.'):
        host = host.removeprefix(prefix)
    path = parsed.path.rstrip('/')
    if host == 'youtu.be':
        return f'youtube:{path.strip("/")}'
    if host == 'youtube.com':
        video = parse_qs(parsed.query).get('v', [''])[0]
        if path == '/watch' and video:
            return f'youtube:{video}'
        for prefix in ('/shorts/', '/embed/', '/live/'):
            if path.startswith(prefix):
                return f'youtube:{path.removeprefix(prefix)}'
    return f'{host}{path.lower()}'


def audiobook_parts(youtube_url, hearthis_url, additional_links):
    """[(część, {'youtube': …, 'hearthis': …})] rosnąco: główne linki to część 1, kolejne z ``additional_links``
    (przy powtórzeniu wygrywa główny link)."""
    parts = {}
    if youtube_url:
        parts.setdefault(1, {})['youtube'] = youtube_url
    if hearthis_url:
        parts.setdefault(1, {})['hearthis'] = hearthis_url
    for link in additional_links or []:
        if isinstance(link, dict) and link.get('url') and link.get('service') in ('youtube', 'hearthis'):
            parts.setdefault(link['part'], {}).setdefault(link['service'], link['url'])
    return sorted(parts.items())


def card_id(audiobook_id, part):
    return f'fza-a{audiobook_id}-{part}'


# --- twórcy -----------------------------------------------------------------------------------------------------------

def user_name(user):
    """Imię i nazwisko z profilu osoby albo z konta – nigdy login ani e-mail."""
    if user is None:
        return ''
    person = getattr(user, 'person_profile', None)
    return (str(person).strip() if person else '') or user.get_full_name().strip()


def proofreading_names(audiobook, stages):
    """Korekta: wykonawcy etapów „Korekta” w kolejności etapów, bez powtórzeń, po przecinku; bez zapisanych
    wykonawców – bieżący korektor audiobooka."""
    names = [user_name(stage.performer) for stage in stages]
    if not any(names):
        names = [user_name(audiobook.proofreader)]
    return ', '.join(dict.fromkeys(name for name in names if name))


def credits(audiobook, stages):
    values = {'lektor': audiobook.narrator_name.strip(), 'korekta': proofreading_names(audiobook, stages),
              'montaz': audiobook.engineer_name.strip()}
    return {key: value for key, value in values.items() if value}


# --- dane dla strony --------------------------------------------------------------------------------------------------

def published_audiobooks():
    proofreading = AudiobookStage.objects.filter(stage_type=StageType.PROOFREADING, performer__isnull=False)
    return Audiobook.objects.filter(status=Audiobook.Status.PUBLISHED).select_related(
        'text__anthology', 'proofreader__person_profile').prefetch_related(
        'text__authors', 'text__translation__foreign_authors', 'site_listen_recordings',
        Prefetch('text__audiobook_stages', to_attr='listen_proofreading',
                 queryset=proofreading.select_related('performer__person_profile').order_by('position', 'pk')))


def _sort_key(audiobook, part):
    """Od najnowszej premiery (na stronę trafiają tylko nagrania z datą premiery), potem tytuł i część."""
    return (-audiobook.premiere_date.toordinal(), fold(audiobook.text.title), audiobook.pk, part)


def _empty_warnings():
    return {'no_links': [], 'no_premiere': [], 'no_entry': {}, 'no_cover': {}, 'disabled': {}, 'skipped_kind': [], 'no_author': [],
            'no_time': [], 'unmatched': [], 'stale': [], 'duplicate_ids': []}


def listen_data():
    """Antologie z nagraniami w formacie strony, liczniki zakładki i ostrzeżenia – jedno przejście po danych.

    Na stronę trafia audiobook o statusie „Opublikowane” z datą premiery i co najmniej jednym linkiem.
    Pomijane (z ostrzeżeniem): opublikowany audiobook bez linków albo bez daty premiery, bez autora, w antologii bez wpisu „Słuchaj”, bez
    okładki albo wyłączonej, Ekstrakty i powieści (strona ich nie ma). Antologie bez nagrań nie trafiają na stronę.
    """
    entries = list(SiteListenAnthology.objects.select_related('anthology').order_by('position', 'pk'))
    by_cms = {entry.anthology_id: entry for entry in entries if entry.anthology_id}
    groups = {entry.pk: [] for entry in entries}
    warnings = _empty_warnings()
    seen_ids, emitted = set(), set()
    for audio in published_audiobooks():
        text = audio.text
        anthology = text.anthology
        label = f'{anthology.title if anthology else "bez antologii"}: {text.title}'
        parts = audiobook_parts(audio.youtube_url, audio.hearthis_url, audio.additional_links)
        if not parts:
            warnings['no_links'].append(label)
        if audio.premiere_date is None:
            warnings['no_premiere'].append(label)
        if not parts or audio.premiere_date is None:
            continue
        if anthology is None or anthology.is_extracts or anthology.is_novel:
            warnings['skipped_kind'].append(label)
            continue
        entry = by_cms.get(anthology.pk)
        reason = 'no_entry' if entry is None else 'disabled' if not entry.is_enabled else '' if entry.cover_url else 'no_cover'
        if reason:
            warnings[reason][anthology.title] = warnings[reason].get(anthology.title, 0) + len(parts)
            continue
        author = text_authors(text)
        if not author:
            warnings['no_author'].append(label)
            continue
        recordings = {recording.part: recording for recording in audio.site_listen_recordings.all()}
        people = credits(audio, text.listen_proofreading)
        for part, links in parts:
            recording = recordings.get(part)
            title = f'{text.title}, cz. {part}' if len(parts) > 1 else text.title
            item = {'id': recording.card_id if recording and recording.card_id else card_id(audio.pk, part),
                    'tytul': title, 'autor': author, 'youtube': links.get('youtube', ''),
                    'hearthis': links.get('hearthis', ''),
                    'czasYoutube': recording.youtube_duration if recording and 'youtube' in links else '',
                    'czasHearthis': recording.hearthis_duration if recording and 'hearthis' in links else '', **people}
            if item['id'] in seen_ids:
                warnings['duplicate_ids'].append(f'{entry.display_title}: {title} ({item["id"]})')
                continue
            seen_ids.add(item['id'])
            emitted.add((audio.pk, part))
            if (item['youtube'] and not item['czasYoutube']) or (item['hearthis'] and not item['czasHearthis']):
                warnings['no_time'].append(f'{entry.display_title}: {title}')
            groups[entry.pk].append((_sort_key(audio, part), {key: value for key, value in item.items() if value}))
    for recording in SiteListenRecording.objects.select_related('listen_anthology__anthology', 'audiobook__text').order_by(
            'listen_anthology__position', 'listen_anthology', 'pk'):
        name = recording.listen_anthology.display_title
        if recording.audiobook_id is None:
            warnings['unmatched'].append(f'{name}: {recording.source_title or recording.card_id}'
                                         f'{f" – {recording.source_author}" if recording.source_author else ""}'
                                         f' ({recording.card_id})')
        elif (recording.audiobook_id, recording.part) not in emitted:
            warnings['stale'].append(f'{name}: {recording.audiobook.text.title}, '
                                     f'cz. {recording.part} ({recording.card_id})')
    anthologies = [{'id': entry.slug, 'nazwa': entry.display_title, 'okladka': entry.cover_url,
                    'nagrania': [item for _, item in sorted(groups[entry.pk], key=lambda row: row[0])]}
                   for entry in entries if groups[entry.pk]]
    for key in ('no_entry', 'no_cover', 'disabled'):
        warnings[key] = [f'{title} ({count_text(count)})' for title, count in sorted(warnings[key].items())]
    return {'antologie': anthologies, 'liczniki': listen_counts(anthologies), 'ostrzezenia': warnings}


def count_text(count):
    return f'{count} {plural(count, *RECORDING_FORMS)}'


def listen_counts(anthologies):
    """Liczniki zakładki: antologie i nagrania na stronie, z YouTube, z HearThis, bez czasu dostępnej platformy
    i bez żadnego twórcy."""
    records = [record for item in anthologies for record in item['nagrania']]
    return {
        'antologie': len(anthologies), 'nagrania': len(records),
        'youtube': sum(1 for record in records if record.get('youtube')),
        'hearthis': sum(1 for record in records if record.get('hearthis')),
        'bez_czasu': sum(1 for record in records if (record.get('youtube') and not record.get('czasYoutube'))
                         or (record.get('hearthis') and not record.get('czasHearthis'))),
        'bez_tworcow': sum(1 for record in records if not any(record.get(key) for key in CREDIT_KEYS)),
    }


def listen_payload(data=None):
    """Słownik wysyłany stronie: dokładnie format bloku ``#fza-audio-data`` plus ``liczby`` i ``wersja`` (skrót treści)."""
    data = data or listen_data()
    return with_version({'antologie': data['antologie'],
                         'liczby': {'antologie': data['liczniki']['antologie'], 'nagrania': data['liczniki']['nagrania']}})


def record_listen_fetch(version):
    SiteTeamSettings.load()
    SiteTeamSettings.objects.filter(pk=1).update(listen_fetched_at=timezone.now(), listen_fetch_version=version[:40])


# --- antologie spoza strony -------------------------------------------------------------------------------------------

def candidate_anthologies():
    """Antologie CMS-u (bez Ekstraktów i powieści) z opublikowanym audiobookiem z datą premiery, których nie ma
    na stronie."""
    return Anthology.objects.filter(
        is_extracts=False, is_novel=False, site_listen__isnull=True,
        texts__audiobook__status=Audiobook.Status.PUBLISHED, texts__audiobook__premiere_date__isnull=False).distinct().order_by('title', 'pk')


def unique_slug(title):
    """Identyfikator antologii na stronie z nazwy (ł → l, bez diakrytyków); przy zajętym – z dopiskiem -2, -3…"""
    base = slugify(title.replace('ł', 'l').replace('Ł', 'L'))[:90] or 'antologia'
    slug, number = base, 1
    while SiteListenAnthology.objects.filter(slug=slug).exists():
        number += 1
        slug = f'{base}-{number}'
    return slug


def add_anthology(anthology, cover_url):
    """Dodaje antologię CMS-u na koniec strony (włączoną)."""
    last = SiteListenAnthology.objects.order_by('-position').values_list('position', flat=True).first() or 0
    return SiteListenAnthology.objects.create(anthology=anthology, slug=unique_slug(anthology.title),
                                              title=anthology.title, cover_url=cover_url, position=last + 1)


# --- dopasowanie seedu do CMS-u ---------------------------------------------------------------------------------------

def link_listen_to_cms(apps=None):
    """Dopasowuje wpisy bez powiązania: antologie strony do antologii CMS-u (po nazwie, rzymskie I–IV = 1–4),
    nagrania do audiobooków – po adresie YouTube albo HearThis (także części z ``additional_links``), a gdy się nie
    da – po antologii i tytule (dopisek „, cz. N” wskazuje część). Zajęte części i niejednoznaczne tytuły zostają
    bez powiązania (ostrzeżenie w zakładce).

    ``apps`` to rejestr modeli historycznych z migracji; bez niego używane są modele bieżące. Zwraca liczby nowo
    powiązanych antologii i nagrań.
    """
    if apps is None:
        anthology_model, audiobook_model = Anthology, Audiobook
        entry_model, recording_model = SiteListenAnthology, SiteListenRecording
    else:
        anthology_model, audiobook_model = apps.get_model('texts', 'Anthology'), apps.get_model('core', 'Audiobook')
        entry_model = apps.get_model('core', 'SiteListenAnthology')
        recording_model = apps.get_model('core', 'SiteListenRecording')
    linked = {'antologie': 0, 'nagrania': 0}
    taken = set(entry_model.objects.exclude(anthology__isnull=True).values_list('anthology_id', flat=True))
    keys = {}
    for pk, title in anthology_model.objects.values_list('pk', 'title'):
        keys.setdefault(anthology_key(title), []).append(pk)
    for pk, title in entry_model.objects.filter(anthology__isnull=True).values_list('pk', 'title'):
        match = keys.get(anthology_key(title), [])
        if len(match) == 1 and match[0] not in taken:
            entry_model.objects.filter(pk=pk).update(anthology_id=match[0])
            taken.add(match[0])
            linked['antologie'] += 1

    by_url, by_title = {}, {}
    rows = audiobook_model.objects.values_list('pk', 'text__anthology_id', 'text__title', 'youtube_url', 'hearthis_url',
                                               'additional_links')
    for pk, anthology_id, title, youtube_url, hearthis_url, additional in rows:
        for part, links in audiobook_parts(youtube_url, hearthis_url, additional):
            for url in links.values():
                by_url.setdefault(media_key(url), (pk, part))
        by_title.setdefault((anthology_id, fold(title)), []).append(pk)
    used = set(recording_model.objects.exclude(audiobook__isnull=True).values_list('audiobook_id', 'part'))
    anthology_of = dict(entry_model.objects.values_list('pk', 'anthology_id'))
    unmatched = recording_model.objects.filter(audiobook__isnull=True).values_list(
        'pk', 'listen_anthology_id', 'source_title', 'source_youtube', 'source_hearthis')
    for pk, entry_id, title, youtube_url, hearthis_url in list(unmatched):
        match = next((by_url[key] for key in (media_key(youtube_url), media_key(hearthis_url))
                      if key and key in by_url), None)
        if match is None and anthology_of.get(entry_id):
            suffix = PART_SUFFIX_RE.search(title or '')
            base = PART_SUFFIX_RE.sub('', title or '') if suffix else title
            found = by_title.get((anthology_of[entry_id], fold(base)), [])
            if len(found) == 1:
                match = (found[0], int(suffix.group(1)) if suffix else 1)
        if match and match not in used:
            recording_model.objects.filter(pk=pk).update(audiobook_id=match[0], part=match[1])
            used.add(match)
            linked['nagrania'] += 1
    return linked
